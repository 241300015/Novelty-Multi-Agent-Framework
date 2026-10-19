"""Classify Reader failures for exactly one Runtime Debug run.

The diagnostic is strictly observational: it reads tool records, manifests and
artifact files, but never repairs or rewrites any of them.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path
from typing import Any

from .contracts import RuntimeDiagnosticContext

RESEARCH = "research_reference"
SUBJECT = "subject_reference"
_NAMESPACE_DIRS = {RESEARCH: "references", SUBJECT: "subject_references"}
_SECRET = re.compile(
    r"(?i)\b(api[_-]?key|token|secret|password|authorization|cookie)"
    r"\s*[:=]\s*([^\s,;]+)"
)
_MAX_MESSAGE = 500

_ACTIONS = {
    "WRONG_NAMESPACE": "检查 namespace 自动定位及调用链",
    "LOST_MANIFEST_ENTRY": "检查并发合并和落盘事务边界",
    "NEVER_PERSISTED": "检查上游落盘失败或模型臆造 ID",
    "MISSING_CONTENT_FILE": "检查制品写入与 Manifest 更新顺序",
    "SHA256_MISMATCH": "检查字节写入；必要时人工运行换行修复脚本",
    "UNREADABLE_MEDIA_TYPE": "检查获取器与派生文本资产",
    "UTF8_DECODE_ERROR": "检查保存编码或文本派生过程",
    "CHAR_RANGE_ERROR": "检查模型参数与 Reader 边界",
    "SCOPE_REJECTED": "检查作用域绑定，不应自动放宽权限",
    "INCOMPLETE_RECORD": "检查异常收尾和工具记录完整性",
    "OTHER": "查看结构化异常类型与脱敏错误信息",
}
_PRIORITY = (
    "LOST_MANIFEST_ENTRY",
    "WRONG_NAMESPACE",
    "NEVER_PERSISTED",
    "MISSING_CONTENT_FILE",
    "SHA256_MISMATCH",
    "UNREADABLE_MEDIA_TYPE",
    "UTF8_DECODE_ERROR",
    "CHAR_RANGE_ERROR",
    "SCOPE_REJECTED",
    "INCOMPLETE_RECORD",
    "OTHER",
)


def _safe_message(value: Any) -> str | None:
    if value is None:
        return None
    return _SECRET.sub(lambda match: f"{match.group(1)}=***REDACTED***", str(value))[
        :_MAX_MESSAGE
    ]


def _load_manifest(
    workspace: Path, namespace: str, errors: list[str]
) -> dict[str, dict[str, Any]]:
    path = workspace / _NAMESPACE_DIRS[namespace] / "list.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        errors.append(f"{namespace} manifest: {type(exc).__name__}: {_safe_message(exc)}")
        return {}
    if not isinstance(payload, dict) or not isinstance(payload.get("artifacts", []), list):
        errors.append(f"{namespace} manifest: invalid structure")
        return {}
    return {
        str(item["artifact_id"]): item
        for item in payload.get("artifacts", [])
        if isinstance(item, dict) and item.get("artifact_id")
    }


def _disk_matches(workspace: Path, artifact_id: str) -> list[tuple[str, Path]]:
    matches: list[tuple[str, Path]] = []
    if not artifact_id:
        return matches
    for namespace, directory in _NAMESPACE_DIRS.items():
        root = workspace / directory / "documents"
        for path in root.glob("*/*"):
            if path.is_file() and path.stem == artifact_id:
                matches.append((namespace, path))
    return matches


def _message_code(message: str) -> str | None:
    lowered = message.lower()
    mappings = (
        ("sha256 mismatch", "SHA256_MISMATCH"),
        ("content file is missing", "MISSING_CONTENT_FILE"),
        ("is not readable text", "UNREADABLE_MEDIA_TYPE"),
        ("not valid utf-8", "UTF8_DECODE_ERROR"),
        ("char_start", "CHAR_RANGE_ERROR"),
        ("outside request scope", "SCOPE_REJECTED"),
        ("scope rejected", "SCOPE_REJECTED"),
    )
    return next((code for fragment, code in mappings if fragment in lowered), None)


def _classify(
    record: dict[str, Any],
    arguments: dict[str, Any],
    manifests: dict[str, dict[str, dict[str, Any]]],
    workspace: Path,
) -> tuple[str, list[str], list[str], bool]:
    status = record.get("execution_status")
    artifact_id = str(arguments.get("artifact_id") or "")
    manifest_presence = [ns for ns, items in manifests.items() if artifact_id in items]
    disk_matches = _disk_matches(workspace, artifact_id)
    file_presence = bool(disk_matches)
    if (
        not artifact_id
        or not record.get("tool_call_id")
        or status == "RUNNING"
        or status not in {"SUCCESS", "FAILED"}
        or (status == "FAILED" and not isinstance(record.get("error"), dict))
    ):
        return (
            "INCOMPLETE_RECORD",
            manifest_presence,
            [ns for ns, _ in disk_matches],
            file_presence,
        )
    if status == "SUCCESS":
        return (
            "SUCCEEDED",
            manifest_presence,
            [ns for ns, _ in disk_matches],
            file_presence,
        )
    error = record.get("error")
    message = str(error.get("message") or "") if isinstance(error, dict) else ""
    code = _message_code(message)
    if code:
        return code, manifest_presence, [ns for ns, _ in disk_matches], file_presence
    if "unknown artifact_id" in message.lower():
        if manifest_presence:
            code = "WRONG_NAMESPACE"
        elif file_presence:
            code = "LOST_MANIFEST_ENTRY"
        else:
            code = "NEVER_PERSISTED"
        return code, manifest_presence, [ns for ns, _ in disk_matches], file_presence
    return "OTHER", manifest_presence, [ns for ns, _ in disk_matches], file_presence


def _reader_items(record: dict[str, Any], arguments: dict[str, Any]):
    """Expand batch item outcomes without mistaking its absent top-level ID for corruption."""
    requests = arguments.get("reads")
    if not isinstance(requests, list) or not requests:
        return [(record, arguments, None)]
    raw = record.get("raw_result")
    payload = record.get("normalized_result") or (raw.get("payload") if isinstance(raw, dict) else {})
    payload = payload if isinstance(payload, dict) else {}
    failures = payload.get("read_errors")
    failures = failures if isinstance(failures, list) else []
    errors = {item.get("index"): item for item in failures if isinstance(item, dict)}
    successes = payload.get("read_results")
    successes = list(successes) if isinstance(successes, list) else []
    result = []
    for index, request in enumerate(requests):
        item = dict(record)
        request = request if isinstance(request, dict) else {}
        failure = errors.get(index)
        if failure is not None:
            item.update(execution_status="FAILED", error={"type": failure.get("error_type"),
                                                        "message": failure.get("message")})
        elif record.get("execution_status") == "SUCCESS":
            match = next((row for row in successes if isinstance(row, dict) and
                row.get("artifact_id") == request.get("artifact_id") and
                row.get("char_start") == request.get("char_start", 0)), None)
            if match is not None:
                successes.remove(match)
                item.update(execution_status="SUCCESS", error=None)
            elif not (payload.get("reused_result") and payload.get("read_status") == "artifact_eof"):
                item.update(execution_status="RUNNING", error=None)  # Missing per-item terminal fact.
        result.append((item, request, index))
    return result


def inspect_reader_failures(context: RuntimeDiagnosticContext) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []
    manifests = {
        namespace: _load_manifest(context.workspace, namespace, errors)
        for namespace in _NAMESPACE_DIRS
    }
    findings: list[dict[str, Any]] = []
    succeeded = 0
    reader_calls = 0
    reader_items = 0
    incomplete = 0
    tools_dir = context.run_dir / "tools"
    for path in sorted(tools_dir.glob("*.json")):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            errors.append(f"{path.name}: {type(exc).__name__}: {_safe_message(exc)}")
            continue
        if not isinstance(record, dict):
            errors.append(f"{path.name}: tool record is not an object")
            continue
        if record.get("tool_name") != "reader":
            continue
        reader_calls += 1
        arguments = record.get("resolved_arguments") or record.get("agent_arguments") or {}
        if not isinstance(arguments, dict):
            arguments = {}
        for item_record, item_arguments, batch_index in _reader_items(record, arguments):
            reader_items += 1
            code, manifest_presence, disk_namespaces, file_presence = _classify(
                item_record, item_arguments, manifests, context.workspace
            )
            if code == "SUCCEEDED":
                succeeded += 1
                continue
            if code == "INCOMPLETE_RECORD":
                incomplete += 1
            error = item_record.get("error") if isinstance(item_record.get("error"), dict) else {}
            actual_namespaces = manifest_presence or disk_namespaces
            findings.append(
                {
                    "reason_code": code,
                    **({"batch_index": batch_index} if batch_index is not None else {}),
                    "tool_call_id": item_record.get("tool_call_id"),
                    "agent_tool_call_id": item_record.get("agent_tool_call_id"),
                    "stage_name": item_record.get("stage_name"),
                    "execution_status": item_record.get("execution_status"),
                    "artifact_id": item_arguments.get("artifact_id"),
                    "namespace": item_arguments.get("namespace"),
                    "actual_namespace": (
                        actual_namespaces[0] if len(actual_namespaces) == 1 else None
                    ),
                    "manifest_presence": manifest_presence,
                    "file_presence": file_presence,
                    "error_type": error.get("type"),
                    "error_message": _safe_message(error.get("message")),
                    "recommended_action": _ACTIONS[code],
                }
            )
    classification_counts = dict(Counter(item["reason_code"] for item in findings))
    failed = len(findings) - incomplete
    if errors:
        status = "ERROR"
    elif reader_calls == 0:
        status = "INCOMPLETE"
    elif findings:
        status = "WARNING"
    else:
        status = "OK"
    primary = next((code for code in _PRIORITY if classification_counts.get(code)), None)
    summaries = {
        "WRONG_NAMESPACE": "存在 Reader 命名空间定位错误",
        "LOST_MANIFEST_ENTRY": "存在文件已写入但 Manifest 未登记的制品",
        "NEVER_PERSISTED": "存在从未落盘的 Artifact ID",
        "MISSING_CONTENT_FILE": "Manifest 已登记但内容文件缺失",
        "SHA256_MISMATCH": "内容文件与 Manifest 哈希不一致",
        "UNREADABLE_MEDIA_TYPE": "Reader 收到了不可读的媒体类型",
        "UTF8_DECODE_ERROR": "文本制品不是有效 UTF-8",
        "CHAR_RANGE_ERROR": "Reader 字符范围参数无效",
        "SCOPE_REJECTED": "Reader 调用越过授权范围",
        "INCOMPLETE_RECORD": "Reader 调用记录未完整结束",
        "OTHER": "存在尚未结构化归类的 Reader 失败",
    }
    return {
        "diagnostic_name": "reader",
        "schema_version": "1.1",
        "status": status,
        "scope": {"paper_id": context.paper_id, "run_id": context.run_id},
        "counts": {
            "reader_calls": reader_calls,
            "reader_items": reader_items,
            "succeeded": succeeded,
            "failed": failed,
            "incomplete": incomplete,
        },
        "classification_counts": classification_counts,
        "verdict": {
            "primary_code": primary,
            "summary": summaries.get(
                primary,
                "当前 run 没有 Reader 调用"
                if not reader_calls
                else "Reader 调用均成功",
            ),
        },
        "findings": findings,
        "errors": errors,
        "warnings": warnings,
    }


class ReaderFailureDiagnostic:
    name = "reader"
    schema_version = "1.1"

    def inspect(self, context: RuntimeDiagnosticContext) -> dict[str, Any]:
        return inspect_reader_failures(context)


__all__ = ["ReaderFailureDiagnostic", "inspect_reader_failures"]
