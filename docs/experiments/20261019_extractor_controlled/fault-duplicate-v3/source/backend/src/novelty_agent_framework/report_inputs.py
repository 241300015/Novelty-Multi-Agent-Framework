"""报告输入版本登记及编译前门控；不调度业务或修复产物。"""

from __future__ import annotations

import hashlib
import json
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

INDEX_NAME = "report-inputs.json"
ARTIFACT_FILES = (
    ("paper", "paper-input/others/paper.json", "论文输入", True),
    ("input_manifest", "paper-input/content-list.json", "解析状态", False),
    ("points", "novelty-points.json", "最终提取结果", True),
    ("plans", "retrieval-plans.json", "规划与实际检索", True),
    ("candidate_audit", "candidate-audit.json", "候选审计", False),
    ("references", "references/list.json", "检索文献与材料目录", False),
    ("subject_references", "subject_references/list.json", "原论文参考文献目录", False),
    ("evidence", "evidence-cards.json", "证据构建、Validator 与拒卡", True),
    ("reviews", "novelty-reviews.json", "单卡／查新点评审与 Gate A", False),
    ("report", "report.json", "结论、局部成果、补检与限制", True),
)
_DEPENDENCIES = {
    "novelty-points.json": ("paper-input/others/paper.json",),
    "retrieval-plans.json": ("novelty-points.json",),
    "evidence-cards.json": ("retrieval-plans.json",),
    "novelty-reviews.json": ("evidence-cards.json",),
    "report.json": ("novelty-points.json", "retrieval-plans.json", "evidence-cards.json", "novelty-reviews.json"),
}
_LOCK = threading.RLock()


class ReportInputVersionError(ValueError):
    """输入不属于同一份完整、未变更的报告材料。"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _read_index(workspace: Path) -> dict[str, Any]:
    value = json.loads((workspace / INDEX_NAME).read_text(encoding="utf-8"))
    if (not isinstance(value, dict) or value.get("schema_version") != 1
            or not isinstance(value.get("artifacts"), dict)
            or not isinstance(value.get("run_id"), str) or not value["run_id"]
            or not isinstance(value.get("paper_id"), str) or not value["paper_id"]):
        raise ReportInputVersionError("报告版本记录格式或版本不受支持")
    return value


def _save(workspace: Path, index: dict[str, Any]) -> None:
    from .persistence import _atomic_write_json
    _atomic_write_json(workspace / INDEX_NAME, index)


def begin_report_inputs(workspace: Path, paper_id: str, run_id: str | None = None) -> None:
    """新运行清空登记，不将目录中遗留的阶段产物纳入新运行。"""
    with _LOCK:
        _save(workspace, {"schema_version": 1, "paper_id": paper_id,
                         "run_id": run_id or f"run-{uuid.uuid4().hex}",
                         "started_at": _now(), "materials_as_of": None,
                         "status": "collecting", "artifacts": {}})


def _check_run(index: dict[str, Any], run_id: str | None) -> None:
    if run_id is not None and index["run_id"] != run_id:
        raise ReportInputVersionError("报告产物运行 ID 不一致；当前工作目录已被另一运行使用")


def _record(workspace: Path, index: dict[str, Any], relative: str, *, origin: str = "stage") -> None:
    path = workspace / relative
    if not path.resolve().is_relative_to(workspace.resolve()):
        raise ReportInputVersionError(f"报告产物路径超出工作目录：{relative}")
    artifacts = index["artifacts"]
    artifacts[relative] = {
        "run_id": index["run_id"], "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "recorded_at": _now(), "origin": origin,
        "dependencies": {name: (artifacts.get(name) or {}).get("sha256")
                         for name in _DEPENDENCIES.get(relative, ())},
    }
    index["status"] = "collecting"
    index["materials_as_of"] = None


def record_report_input_files(workspace: Path, relatives: tuple[str, ...], *, run_id: str | None = None) -> None:
    """登记本次明确接收的论文输入，不扫描其他阶段文件。"""
    with _LOCK:
        index = _read_index(workspace)
        _check_run(index, run_id)
        for relative in relatives:
            _record(workspace, index, relative, origin="input")
        _save(workspace, index)


def write_report_artifact(workspace: Path, path: Path, payload: Any, *, run_id: str | None = None) -> None:
    """在阶段写入时记录版本；无运行入口的历史调用只保留原有落盘行为。"""
    from .persistence import _atomic_write_json
    with _LOCK:
        index = _read_index(workspace) if (workspace / INDEX_NAME).exists() else None
        if index is not None:
            _check_run(index, run_id)
        elif run_id is not None:
            raise ReportInputVersionError("缺少报告运行版本记录，请先持久化工作流输入")
        _atomic_write_json(path, payload)
        if index is not None:
            _record(workspace, index, path.relative_to(workspace).as_posix())
            _save(workspace, index)


def finalize_report_inputs(workspace: Path, *, run_id: str | None = None) -> None:
    """报告落盘后封存版本。可复用文献目录作为显式输入，不重登记阶段成果。"""
    with _LOCK:
        if not (workspace / INDEX_NAME).exists():
            return
        index = _read_index(workspace)
        _check_run(index, run_id)
        for relative in ("references/list.json", "subject_references/list.json"):
            if (workspace / relative).is_file():
                _record(workspace, index, relative, origin="reference_input")
            else:
                index["artifacts"][relative] = None
        for _, relative, _, _ in ARTIFACT_FILES:
            index["artifacts"].setdefault(relative, None)
        index["status"] = "ready"
        index["materials_as_of"] = _now()
        _save(workspace, index)


def load_verified_report_inputs(workspace: Path) -> tuple[dict[str, Any], dict[str, bytes]]:
    """核验后返回同一批字节，Renderer 不再重新读取未核验的业务内容。"""
    try:
        if not (workspace / INDEX_NAME).is_file():
            raise ReportInputVersionError("缺少报告版本记录 report-inputs.json；旧目录需重新落盘运行产物")
        index = _read_index(workspace)
        if index.get("status") != "ready":
            raise ReportInputVersionError("报告材料尚未封存，不能编译")
        start = datetime.fromisoformat(index["started_at"])
        end = datetime.fromisoformat(index["materials_as_of"])
        if start.tzinfo is None or end.tzinfo is None or start > end:
            raise ReportInputVersionError("材料所属时间无效")
        artifacts = index["artifacts"]
        for _, relative, _, required in ARTIFACT_FILES:
            if required and not artifacts.get(relative):
                raise ReportInputVersionError(f"缺少本次运行的报告输入版本：{relative}")
            if not artifacts.get(relative) and (workspace / relative).exists():
                raise ReportInputVersionError(f"发现未登记或旧运行产物：{relative}")
        content = {}
        for relative, entry in artifacts.items():
            if entry is None:
                continue
            if not isinstance(entry, dict) or entry.get("run_id") != index["run_id"]:
                raise ReportInputVersionError(f"报告产物运行 ID 不一致：{relative}")
            path = workspace / relative
            if Path(relative).is_absolute() or not path.resolve().is_relative_to(workspace.resolve()):
                raise ReportInputVersionError(f"报告产物路径超出工作目录：{relative}")
            raw = path.read_bytes()
            if hashlib.sha256(raw).hexdigest() != entry.get("sha256"):
                raise ReportInputVersionError(f"报告产物版本不一致：{relative}")
            dependencies = entry.get("dependencies")
            if not isinstance(dependencies, dict) or set(dependencies) != set(_DEPENDENCIES.get(relative, ())):
                raise ReportInputVersionError(f"报告产物依赖版本记录不完整：{relative}")
            for dependency, digest in dependencies.items():
                if (artifacts.get(dependency) or {}).get("sha256") != digest:
                    raise ReportInputVersionError(f"报告产物依赖版本已变化：{relative} → {dependency}")
            content[relative] = raw
        if _read_index(workspace) != index:
            raise ReportInputVersionError("报告核验期间版本记录发生变化，请重试")
        return index, content
    except ReportInputVersionError:
        raise
    except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
        raise ReportInputVersionError(f"报告版本核验失败：{exc}") from exc
