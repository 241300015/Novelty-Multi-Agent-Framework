"""按论文组织的本地运行产物持久化。

当前实现面向开发和测试，把每篇论文的阶段产物写入
``outputs/<paper_id>/``。生产环境后续可替换为对象存储或数据库，但工作流
只应通过本模块写出产物，避免各节点自行拼接路径。
"""

from __future__ import annotations

from .schemas.failures import FailureCode as C, CodedFailure

import json
import hashlib
import os
import re
import shutil
import tempfile
import threading
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from .schemas import (
    AccessStatus,
    Artifact,
    ArtifactNamespace,
    EvidenceCard,
    EvidenceReviewDecision,
    NoveltyPoint,
    NoveltyPointReview,
    NoveltyReport,
    PaperDocument,
    PaperInput,
    ReferenceBootstrapManifest,
    ReferenceManifest,
    ReferenceNamespace,
    ReferenceReadResult,
    RejectedEvidence,
    ResearchTask,
    SearchPlan,
    SourceRecord,
    TaskResearchResult,
    Work,
)

DEFAULT_OUTPUTS_DIR = Path("outputs")
STORAGE_VERSION = "test-version-local-file"

# Manifest 是「读-改-写」共享状态：多个 Researcher 任务并发检索时，
# 若各自先 load、隔一段 await 后再 persist 整份覆盖，就会互相抹掉条目
# （落盘文件仍在，但 list.json 里没有记录，reader 随即报 unknown artifact_id）。
# 所有 manifest 写入共用一把可重入锁，并且合并必须在锁内重新读取。
_MANIFEST_WRITE_LOCK = threading.RLock()


def merge_reference_manifest(
    manifest: ReferenceManifest,
    *,
    works: Sequence[Work] = (),
    source_records: Sequence[SourceRecord] = (),
    artifacts: Sequence[Artifact] = (),
) -> ReferenceManifest:
    """把新增的 Work/SourceRecord/Artifact 合并进既有 Manifest。"""

    work_map = {item.work_id: item for item in manifest.works}
    record_map = {item.source_record_id: item for item in manifest.source_records}
    artifact_map = {item.artifact_id: item for item in manifest.artifacts}
    for work in works:
        merge_work(work_map, work)
    for record in source_records:
        merge_record(record_map, record)
    for artifact in artifacts:
        existing = artifact_map.get(artifact.artifact_id)
        if existing is not None and (
            existing.work_id != artifact.work_id
            or existing.sha256 != artifact.sha256
            or existing.relative_path != artifact.relative_path
        ):
            raise ValueError(f"artifact {artifact.artifact_id} conflicts with manifest")
        artifact_map.setdefault(artifact.artifact_id, artifact)
    return ReferenceManifest(
        schema_version=manifest.schema_version,
        subject_paper_id=manifest.subject_paper_id,
        updated_at=datetime.now(timezone.utc),
        works=list(work_map.values()),
        source_records=list(record_map.values()),
        artifacts=list(artifact_map.values()),
    )


def merge_work(target: dict[str, Work], value: Work) -> None:
    if value.work_id not in target:
        target[value.work_id] = value


def merge_record(target: dict[str, SourceRecord], value: SourceRecord) -> None:
    existing = target.get(value.source_record_id)
    if existing is None:
        target[value.source_record_id] = value
        return
    if existing.source_id != value.source_id:
        raise ValueError(f"source_record {value.source_record_id} has identity conflict")
    if (
        existing.work_id is not None
        and value.work_id is not None
        and existing.work_id != value.work_id
    ):
        raise ValueError(f"source_record {value.source_record_id} has identity conflict")
    updates: dict[str, Any] = {}
    if existing.work_id is None and value.work_id is not None:
        # 记录可以先入库、之后由 Browser 等工具补上 work_id。
        updates["work_id"] = value.work_id
    if value.access_status == AccessStatus.FULL_TEXT_ACQUIRED:
        updates["access_status"] = value.access_status
    if updates:
        target[value.source_record_id] = existing.model_copy(update=updates)


def paper_workspace(
    paper: PaperInput | PaperDocument | str,
    *,
    output_root: str | Path = DEFAULT_OUTPUTS_DIR,
) -> Path:
    """创建并返回单篇论文工作目录，阻止 paper_id 逃逸输出根目录。"""

    paper_id = paper if isinstance(paper, str) else paper.paper_id
    safe_name = _safe_directory_name(paper_id)
    workspace = Path(output_root) / safe_name
    for directory in (
        workspace / "paper-input" / "images",
        workspace / "paper-input" / "others",
        workspace / "references" / "documents",
        workspace / "subject_references" / "documents",
        workspace / "report",
    ):
        directory.mkdir(parents=True, exist_ok=True)
    manifest_path = workspace / "references" / "list.json"
    if not manifest_path.exists():
        manifest = ReferenceManifest(
            subject_paper_id=paper_id,
            updated_at=datetime.now(timezone.utc),
        )
        _atomic_write_json(manifest_path, manifest.model_dump(mode="json"))
    subject_manifest_path = workspace / "subject_references" / "list.json"
    if not subject_manifest_path.exists():
        manifest = ReferenceManifest(
            subject_paper_id=paper_id,
            updated_at=datetime.now(timezone.utc),
        )
        _atomic_write_json(subject_manifest_path, manifest.model_dump(mode="json"))
    return workspace


def reference_workspace(
    paper: PaperInput | PaperDocument | str,
    *,
    output_root: str | Path = DEFAULT_OUTPUTS_DIR,
) -> Path:
    """返回论文工作区内的参考文献目录，并确保其已经初始化。"""

    return paper_workspace(paper, output_root=output_root) / "references"


def subject_reference_workspace(
    paper: PaperInput | PaperDocument | str,
    *,
    output_root: str | Path = DEFAULT_OUTPUTS_DIR,
) -> Path:
    return paper_workspace(paper, output_root=output_root) / "subject_references"


def reference_documents_dir(
    paper: PaperInput | PaperDocument | str,
    *,
    output_root: str | Path = DEFAULT_OUTPUTS_DIR,
) -> Path:
    """返回只能由持久化层生成的参考文献制品根目录。"""

    return reference_workspace(paper, output_root=output_root) / "documents"


def load_reference_manifest(
    paper: PaperInput | PaperDocument | str,
    *,
    output_root: str | Path = DEFAULT_OUTPUTS_DIR,
) -> ReferenceManifest:
    """读取并重新校验 references/list.json，不吞掉损坏数据。"""

    path = reference_workspace(paper, output_root=output_root) / "list.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    return ReferenceManifest.model_validate(payload)


def persist_reference_manifest(
    paper: PaperInput | PaperDocument | str,
    manifest: ReferenceManifest,
    *,
    output_root: str | Path = DEFAULT_OUTPUTS_DIR,
) -> Path:
    """校验并原子替换论文参考文献清单。"""

    paper_id = paper if isinstance(paper, str) else paper.paper_id
    if manifest.subject_paper_id != paper_id:
        raise ValueError(
            "manifest.subject_paper_id "
            f"{manifest.subject_paper_id!r} does not match paper_id {paper_id!r}"
        )
    validated = ReferenceManifest.model_validate(manifest.model_dump(mode="python"))
    path = reference_workspace(paper, output_root=output_root) / "list.json"
    _atomic_write_json(path, validated.model_dump(mode="json"))
    return path


class ReferenceStore:
    """参考文献 Manifest 与制品文件的单一工作区存储入口。"""

    def __init__(
        self,
        output_root: str | Path = DEFAULT_OUTPUTS_DIR,
        namespace: ReferenceNamespace = ReferenceNamespace.RESEARCH,
    ) -> None:
        self.output_root = Path(output_root)
        self.namespace = ReferenceNamespace(namespace)

    def _workspace(self, paper_id: str) -> Path:
        if self.namespace == ReferenceNamespace.SUBJECT_REFERENCE:
            return subject_reference_workspace(paper_id, output_root=self.output_root)
        return reference_workspace(paper_id, output_root=self.output_root)

    def load_manifest(self, paper_id: str) -> ReferenceManifest:
        path = self._workspace(paper_id) / "list.json"
        return ReferenceManifest.model_validate(
            json.loads(path.read_text(encoding="utf-8"))
        )

    def persist_manifest(
        self, paper_id: str, manifest: ReferenceManifest
    ) -> Path:
        if manifest.subject_paper_id != paper_id:
            raise ValueError("manifest subject_paper_id does not match paper_id")
        path = self._workspace(paper_id) / "list.json"
        with _MANIFEST_WRITE_LOCK:
            _atomic_write_json(
                path,
                ReferenceManifest.model_validate(manifest).model_dump(mode="json"),
            )
        return path

    def merge_manifest(
        self,
        paper_id: str,
        *,
        works: Sequence[Work] = (),
        source_records: Sequence[SourceRecord] = (),
        artifacts: Sequence[Artifact] = (),
    ) -> ReferenceManifest:
        """在锁内「重新读取 → 合并 → 写回」，避免并发写入互相覆盖。

        调用方不应先 load_manifest 再 persist_manifest：两次调用之间其它任务的
        更新会被整份覆盖掉，留下「文件已落盘但 manifest 无记录」的悬空制品。
        """

        with _MANIFEST_WRITE_LOCK:
            merged = merge_reference_manifest(
                self.load_manifest(paper_id),
                works=works,
                source_records=source_records,
                artifacts=artifacts,
            )
            self.persist_manifest(paper_id, merged)
            return merged

    def write_document(
        self,
        paper_id: str,
        *,
        work_id: str,
        artifact_id: str,
        extension: str,
        content: str,
    ) -> Path:
        """原子写入 UTF-8 文本制品，路径段只接受内部安全 ID。"""

        safe_work_id = _safe_storage_id(work_id, "work_id")
        safe_artifact_id = _safe_storage_id(artifact_id, "artifact_id")
        suffix = extension.strip().lower().lstrip(".")
        if not re.fullmatch(r"[a-z0-9]+", suffix):
            raise ValueError("artifact extension is unsafe")
        directory = self._workspace(paper_id) / "documents" / safe_work_id
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{safe_artifact_id}.{suffix}"
        if path.exists():
            if path.read_text(encoding="utf-8") != content:
                raise ValueError(f"artifact {artifact_id} content conflicts with existing file")
            return path
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                newline="\n",
                dir=directory,
                prefix=f".{safe_artifact_id}.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                temporary_path = Path(handle.name)
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_path, path)
            temporary_path = None
            return path
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)

    def read_document_slice(
        self,
        paper_id: str,
        *,
        artifact_id: str,
        char_start: int,
        max_chars: int,
    ) -> ReferenceReadResult:
        """仅按 Manifest 中的文本 Artifact ID 读取受限字符片段。"""

        artifact, _path, raw = self.verify_artifact_file(paper_id, artifact_id)
        if artifact.media_type not in {
            "text/plain",
            "text/markdown",
            "text/html",
            "application/json",
        }:
            raise ValueError(
                f"artifact {artifact_id} media_type {artifact.media_type!r} is not readable text"
            )
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError(f"artifact {artifact_id} is not valid UTF-8 text") from exc
        if char_start > len(text):
            raise ValueError(
                f"char_start {char_start} exceeds artifact {artifact_id} length"
            )
        char_end = min(len(text), char_start + max_chars)
        read_id = "read_" + hashlib.sha256(
            (
                f"{artifact_namespace_for_reference_namespace(self.namespace).value}"
                f"\x1f{artifact_id}\x1f{char_start}\x1f{char_end}\x1f{artifact.sha256}"
            ).encode()
        ).hexdigest()[:24]
        return ReferenceReadResult(
            namespace=artifact_namespace_for_reference_namespace(self.namespace),
            read_id=read_id,
            work_id=artifact.work_id,
            artifact_id=artifact.artifact_id,
            role=artifact.role,
            char_start=char_start,
            char_end=char_end,
            text=text[char_start:char_end],
            has_more=char_end < len(text),
            sha256=artifact.sha256,
        )

    def find_artifact(self, paper_id: str, artifact_id: str) -> Artifact | None:
        """只查 Manifest 的制品索引，不触碰磁盘；不存在时返回 None。"""

        return next(
            (
                item
                for item in self.load_manifest(paper_id).artifacts
                if item.artifact_id == artifact_id
            ),
            None,
        )

    def verify_artifact_file(
        self,
        paper_id: str,
        artifact_id: str,
    ) -> tuple[Artifact, Path, bytes]:
        """Resolve one manifest Artifact and verify path, existence and SHA-256."""

        artifact = self.find_artifact(paper_id, artifact_id)
        if artifact is None:
            # 带上查的是哪个命名空间：模型常把研究语料与论文自带参考语料的
            # artifact_id 混用，明确的提示能让它下一轮自己改对 namespace。
            raise CodedFailure(C.READER_ARTIFACT_UNREGISTERED,
                f"unknown artifact_id {artifact_id!r} in the "
                f"{self.namespace.value} manifest"
            )
        references_dir = self._workspace(paper_id).resolve()
        path = (references_dir / artifact.relative_path).resolve()
        if not path.is_relative_to(references_dir):
            raise CodedFailure(C.READER_PATH_ESCAPE, f"artifact {artifact_id} path escapes references workspace")
        if not path.is_file():
            raise FileNotFoundError(f"artifact {artifact_id} content file is missing")
        raw = path.read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        if digest != artifact.sha256:
            raise CodedFailure(C.READER_HASH_MISMATCH, f"artifact {artifact_id} sha256 mismatch")
        return artifact, path, raw


class SubjectReferenceStore(ReferenceStore):
    """论文自身参考文献语料库的独立命名空间存储。"""

    def __init__(self, output_root: str | Path = DEFAULT_OUTPUTS_DIR) -> None:
        super().__init__(output_root, namespace=ReferenceNamespace.SUBJECT_REFERENCE)

    def load_bootstrap(self, paper_id: str) -> ReferenceBootstrapManifest:
        path = self._workspace(paper_id) / "bootstrap.json"
        if not path.exists():
            return ReferenceBootstrapManifest(subject_paper_id=paper_id)
        return ReferenceBootstrapManifest.model_validate_json(
            path.read_text(encoding="utf-8")
        )

    def persist_bootstrap(
        self, paper_id: str, manifest: ReferenceBootstrapManifest
    ) -> Path:
        if manifest.subject_paper_id != paper_id:
            raise ValueError("bootstrap subject_paper_id does not match paper_id")
        path = self._workspace(paper_id) / "bootstrap.json"
        _atomic_write_json(path, manifest.model_dump(mode="json"))
        return path


def artifact_namespace_for_reference_namespace(
    namespace: ReferenceNamespace,
) -> ArtifactNamespace:
    """Map a physical ReferenceStore namespace to its public artifact address."""

    mapping = {
        ReferenceNamespace.RESEARCH: ArtifactNamespace.RESEARCH_REFERENCE,
        ReferenceNamespace.SUBJECT_REFERENCE: ArtifactNamespace.SUBJECT_REFERENCE,
    }
    return mapping[ReferenceNamespace(namespace)]


def reference_store_for_artifact_namespace(
    namespace: ArtifactNamespace,
    *,
    output_root: str | Path = DEFAULT_OUTPUTS_DIR,
) -> ReferenceStore:
    if namespace == ArtifactNamespace.SUBJECT_REFERENCE:
        return SubjectReferenceStore(output_root)
    return ReferenceStore(output_root)


def persist_paper_input(
    document: PaperDocument,
    paper: PaperInput,
    *,
    output_root: str | Path = DEFAULT_OUTPUTS_DIR,
) -> Path:
    """写出论文全文、内容清单和供工作流重载的结构化输入。"""

    workspace = paper_workspace(document, output_root=output_root)
    input_dir = workspace / "paper-input"
    (input_dir / "full.md").write_text(document.full_text + "\n", encoding="utf-8")

    document.images = _copy_paper_images(document.images, input_dir / "images")
    paper.images = list(document.images)

    content_list = {
        "paper_id": document.paper_id,
        "title": document.title,
        "source": document.source,
        "page_count": len(document.pages),
        "sections": [
            {
                "name": name,
                "present": bool(content.strip()),
                "characters": len(content),
            }
            for name, content in document.sections.items()
        ],
        "keywords_zh": document.keywords_zh,
        "keywords_en": document.keywords_en,
        "reference_count": len(document.references),
        "parse_warnings": document.parse_warnings,
        "workflow_input": "others/paper.json",
        "images": [item.model_dump(mode="json") for item in document.images],
        "tables": [item.model_dump(mode="json") for item in document.tables],
        "equations": [item.model_dump(mode="json") for item in document.equations],
    }
    _write_json(input_dir / "content-list.json", content_list)
    _write_json(
        input_dir / "others" / "paper.json",
        paper.model_dump(mode="json"),
    )
    return workspace


def persist_workflow_input(
    paper: PaperInput,
    *,
    output_root: str | Path = DEFAULT_OUTPUTS_DIR,
) -> Path:
    """确保直接通过 JSON/API 启动的工作流也具备完整 paper-input 目录。"""

    workspace = paper_workspace(paper, output_root=output_root)
    input_dir = workspace / "paper-input"
    full_path = input_dir / "full.md"
    content_list_path = input_dir / "content-list.json"
    if not full_path.exists():
        full_path.write_text(paper.full_text + "\n", encoding="utf-8")
    if not content_list_path.exists():
        _write_json(
            content_list_path,
            {
                "paper_id": paper.paper_id,
                "title": paper.title,
                "source": paper.metadata.get("source", "workflow_input"),
                "page_count": int(paper.metadata.get("pages", "0") or 0),
                "sections": [],
                "keywords_zh": paper.keywords_zh,
                "keywords_en": paper.keywords_en,
                "reference_count": len(paper.references),
                "parse_warnings": [],
                "workflow_input": "others/paper.json",
                "images": [item.model_dump(mode="json") for item in paper.images],
                "tables": [item.model_dump(mode="json") for item in paper.tables],
                "equations": [
                    item.model_dump(mode="json") for item in paper.equations
                ],
            },
        )
    _write_json(
        input_dir / "others" / "paper.json",
        paper.model_dump(mode="json"),
    )
    return workspace


def persist_novelty_points(
    paper: PaperInput,
    points: Sequence[NoveltyPoint],
    *,
    output_root: str | Path = DEFAULT_OUTPUTS_DIR,
) -> Path:
    """把生成的查新点写入论文工作目录。"""

    workspace = paper_workspace(paper, output_root=output_root)
    path = workspace / "novelty-points.json"
    _write_json(
        path,
        {
            "paper_id": paper.paper_id,
            "title": paper.title,
            "storage": STORAGE_VERSION,
            "novelty_points": [point.model_dump(mode="json") for point in points],
        },
    )
    return path


def persist_retrieval_plans(
    paper: PaperInput,
    tasks: Sequence[ResearchTask],
    *,
    search_plans: Sequence[SearchPlan] = (),
    executed_queries: Sequence[Mapping[str, Any]] = (),
    rounds: int,
    point_order: Sequence[str] = (),
    output_root: str | Path = DEFAULT_OUTPUTS_DIR,
) -> Path:
    """按查新点写出任务、语义计划及真正执行的数据库 Query。"""

    workspace = paper_workspace(paper, output_root=output_root)
    path = workspace / "retrieval-plans.json"
    grouped: dict[str, list[ResearchTask]] = {}
    for task in tasks:
        grouped.setdefault(task.novelty_point_id, []).append(task)
    grouped_plans: dict[str, list[SearchPlan]] = {}
    for plan in search_plans:
        grouped_plans.setdefault(plan.novelty_point_id, []).append(plan)
    grouped_queries: dict[str, list[Mapping[str, Any]]] = {}
    for query in executed_queries:
        point_id = str(query.get("novelty_point_id", ""))
        grouped_queries.setdefault(point_id, []).append(query)

    ordered_point_ids = list(
        dict.fromkeys([*point_order, *grouped, *grouped_plans, *grouped_queries])
    )
    plans = []
    for sequence, point_id in enumerate(ordered_point_ids, start=1):
        point_tasks = grouped.get(point_id, [])
        point_plans = grouped_plans.get(point_id, [])
        point_queries = grouped_queries.get(point_id, [])
        plans.append(
            {
                "sequence": sequence,
                "novelty_point_id": point_id,
                "research_tasks": [
                    task.model_dump(mode="json") for task in point_tasks
                ],
                "search_plans": [
                    plan.model_dump(mode="json") for plan in point_plans
                ],
                "executed_queries": [dict(query) for query in point_queries],
                "query_plan": {
                    "queries": _unique(
                        str(query.get("query", "")) for query in point_queries
                    ),
                    "attempts": sorted({task.attempt for task in point_tasks}),
                },
            }
        )
    _write_json(
        path,
        {
            "paper_id": paper.paper_id,
            "rounds": rounds,
            "novelty_point_plans": plans,
        },
    )
    return path


def persist_evidence_cards(
    paper: PaperInput,
    *,
    raw_cards: Sequence[EvidenceCard],
    accepted_cards: Sequence[EvidenceCard],
    rejected_evidence: Sequence[RejectedEvidence],
    validator_accepted_cards: Sequence[EvidenceCard] | None = None,
    review_decisions: Sequence[EvidenceReviewDecision] | None = None,
    output_root: str | Path = DEFAULT_OUTPUTS_DIR,
) -> Path:
    """写出证据校验前后结果，保留拒绝原因供审计。"""

    workspace = paper_workspace(paper, output_root=output_root)
    path = workspace / "evidence-cards.json"
    payload: dict[str, Any] = {
        "paper_id": paper.paper_id,
        "raw_evidence_cards": [card.model_dump(mode="json") for card in raw_cards],
        "accepted_evidence_cards": [
            card.model_dump(mode="json") for card in accepted_cards
        ],
        "rejected_evidence": [
            item.model_dump(mode="json") for item in rejected_evidence
        ],
    }
    if validator_accepted_cards is not None:
        payload["validator_accepted_cards"] = [
            card.model_dump(mode="json") for card in validator_accepted_cards
        ]
    if review_decisions is not None:
        payload["review_decisions"] = [
            decision.model_dump(mode="json") for decision in review_decisions
        ]
    _write_json(path, payload)
    return path


def persist_novelty_reviews(
    paper: PaperInput,
    reviews: Sequence[NoveltyPointReview],
    *,
    output_root: str | Path = DEFAULT_OUTPUTS_DIR,
    card_reviews: Sequence[dict[str, Any]] | None = None,
    phase: str = "complete",
    integrity: dict[str, Any] | None = None,
) -> Path:
    """写出 Reviewer 的正式查新点级业务产物。"""

    workspace = paper_workspace(paper, output_root=output_root)
    path = workspace / "novelty-reviews.json"
    _atomic_write_json(
        path,
        {
            "paper_id": paper.paper_id,
            "reviews": [item.model_dump(mode="json") for item in reviews],
            **({"integrity": integrity} if integrity is not None else {}),
            **({"phase": phase, "card_reviews": list(card_reviews)}
               if card_reviews is not None else {}),
        },
    )
    return path


def persist_task_research_result(
    paper_id: str,
    result: TaskResearchResult,
    *,
    attempt: int,
    output_root: str | Path = DEFAULT_OUTPUTS_DIR,
) -> Path:
    """写出单任务审计，不包含全文或模型隐式推理。"""

    workspace = paper_workspace(paper_id, output_root=output_root)
    point_id = _safe_storage_id(result.novelty_point_id, "novelty_point_id")
    task_id = _safe_storage_id(result.task_id, "task_id")
    directory = workspace / "research-runs" / point_id / task_id
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"attempt-{attempt}.json"
    _atomic_write_json(path, result.model_dump(mode="json"))
    return path


def persist_task_retrieval_audit(
    paper: PaperInput,
    tasks: Sequence[ResearchTask],
    results: Sequence[TaskResearchResult],
    *,
    search_plans: Sequence[SearchPlan] = (),
    rounds: int,
    point_order: Sequence[str] = (),
    output_root: str | Path = DEFAULT_OUTPUTS_DIR,
) -> Path:
    """从任务结果聚合真实 SearchExecution，保留旧 retrieval-plans 文件。"""

    _write_json(
        paper_workspace(paper, output_root=output_root) / "candidate-audit.json",
        {
            "paper_id": paper.paper_id,
            "tasks": [
                {
                    "novelty_point_id": result.novelty_point_id,
                    "task_id": result.task_id,
                    "status": result.status.value,
                    "candidates": [item.model_dump(mode="json") for item in result.candidate_audit],
                }
                for result in results
            ],
        },
    )
    executed_queries: list[dict[str, Any]] = []
    for result in results:
        executions = {(execution.execution_id, execution.started_at): execution for bundle in result.research_bundles
                      for execution in bundle.search_executions}
        executions.update({(execution.execution_id, execution.started_at): execution for execution in result.search_executions})
        for execution in executions.values():
            status = execution.status.value
            result_marker = ("zero_hits" if status == "succeeded" and not execution.results else
                             "has_hits" if status == "succeeded" else status)
            executed_queries.append({
                "database": execution.source_id, "task_id": result.task_id,
                "novelty_point_id": result.novelty_point_id,
                "strategy_id": execution.parameters.get("strategy_id", ""),
                "level": execution.parameters.get("level", ""),
                "query": execution.query, "status": status, "result_marker": result_marker,
                "error": execution.error,
                "results": [item.model_dump(mode="json") for item in execution.results],
            })
    return persist_retrieval_plans(
        paper,
        tasks,
        search_plans=search_plans,
        executed_queries=executed_queries,
        rounds=rounds,
        point_order=point_order,
        output_root=output_root,
    )


def persist_report(
    paper: PaperInput,
    report: NoveltyReport,
    *,
    output_root: str | Path = DEFAULT_OUTPUTS_DIR,
) -> Path:
    """写出 Coordinator.synthesize() 生成并通过校验的结构化报告。"""

    workspace = paper_workspace(paper, output_root=output_root)
    path = workspace / "report.json"
    _atomic_write_json(path, report.model_dump(mode="json"))
    return path


def _copy_paper_images(
    images: Sequence[Any],
    dest_dir: Path,
) -> list[Any]:
    """把 MinerU 产物图片复制到 paper 工作区，并返回路径已更新的图片列表。"""
    dest_dir.mkdir(parents=True, exist_ok=True)
    updated: list[Any] = []
    for index, image in enumerate(images):
        src = Path(image.path)
        if src.exists() and src.is_file():
            dest_name = f"{index:03d}_{src.name}"
            dest = dest_dir / dest_name
            shutil.copy2(src, dest)
            updated.append(image.model_copy(update={"path": f"images/{dest_name}"}))
        else:
            updated.append(image)
    return updated


def _safe_directory_name(paper_id: str) -> str:
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", paper_id.strip()).strip("._")
    if not name:
        raise ValueError("paper_id 无法转换为安全的工作目录名")
    return name


def _safe_storage_id(value: str, field_name: str) -> str:
    value = value.strip()
    if not value or re.fullmatch(r"[A-Za-z0-9_-]+", value) is None:
        raise ValueError(f"{field_name} is not a safe storage identifier")
    return value


def _write_json(path: Path, payload: Any) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def _atomic_write_json(path: Path, payload: Any) -> None:
    """在目标目录落盘后原子替换 JSON，并清理失败的临时文件。"""

    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_path = Path(handle.name)
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
        temporary_path = None
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def _unique(values: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(values))
