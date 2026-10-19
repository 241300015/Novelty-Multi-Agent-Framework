"""只读检查跨 Namespace 参考文献资产地址链。

用法：
    python scripts/reference_namespace_diagnostics.py outputs/<paper_id>
    python scripts/reference_namespace_diagnostics.py outputs/<paper_id> \
        --namespace subject_reference --artifact-id artifact_abc

输出只包含 ID、相对路径、计数和错误，不复制 Reader 正文或 Evidence quote。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "backend" / "src"))

from pydantic import ValidationError

from novelty_agent_framework.schemas import ArtifactNamespace, ReferenceManifest


NAMESPACE_DIRECTORIES = {
    ArtifactNamespace.RESEARCH_REFERENCE: "references",
    ArtifactNamespace.SUBJECT_REFERENCE: "subject_references",
}


def _load_json(path: Path, errors: list[str]) -> Any | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError) as exc:
        errors.append(f"invalid JSON {path}: {type(exc).__name__}: {exc}")
        return None


def _artifact_integrity(workspace: Path, directory: str, artifact) -> dict[str, Any]:
    root = (workspace / directory).resolve()
    path = (root / artifact.relative_path).resolve()
    result = {
        "artifact_id": artifact.artifact_id,
        "work_id": artifact.work_id,
        "source_record_id": artifact.source_record_id,
        "relative_path": artifact.relative_path,
        "integrity": "ok",
        "error": None,
    }
    if not path.is_relative_to(root):
        result.update(integrity="failed", error="path escapes namespace workspace")
        return result
    if not path.is_file():
        result.update(integrity="failed", error="content file is missing")
        return result
    try:
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as exc:
        result.update(integrity="failed", error=f"cannot read file: {exc}")
        return result
    if digest != artifact.sha256:
        result.update(integrity="failed", error="sha256 mismatch")
    return result


def _read_result(payload: dict[str, Any]) -> dict[str, Any]:
    raw = payload.get("raw_result") or {}
    raw_payload = raw.get("payload") or {}
    result = raw_payload.get("read_result")
    if isinstance(result, dict):
        return result
    normalized = payload.get("normalized_result") or {}
    result = normalized.get("read_result")
    return result if isinstance(result, dict) else {}


def inspect_workspace(
    workspace: Path,
    *,
    namespace: ArtifactNamespace | str | None = None,
    artifact_id: str | None = None,
    run_id: str | None = None,
) -> dict[str, Any]:
    """Return deterministic diagnostics without source or quote text."""

    workspace = Path(workspace)
    selected_namespace = ArtifactNamespace(namespace) if namespace is not None else None
    if (selected_namespace is None) != (artifact_id is None):
        raise ValueError("namespace and artifact_id must be provided together")

    errors: list[str] = []
    warnings: list[str] = []
    namespace_reports: dict[str, dict[str, Any]] = {}
    artifacts_by_namespace: dict[ArtifactNamespace, dict[str, dict[str, Any]]] = {}

    for artifact_namespace, directory in NAMESPACE_DIRECTORIES.items():
        manifest_path = workspace / directory / "list.json"
        payload = _load_json(manifest_path, errors)
        if payload is None:
            warnings.append(f"missing namespace manifest: {artifact_namespace.value}")
            namespace_reports[artifact_namespace.value] = {
                "manifest": str(manifest_path),
                "valid": False,
                "works": 0,
                "source_records": 0,
                "artifacts": 0,
                "integrity_failures": [],
            }
            artifacts_by_namespace[artifact_namespace] = {}
            continue
        try:
            manifest = ReferenceManifest.model_validate(payload)
        except ValidationError as exc:
            message = exc.errors(include_url=False)[0]["msg"]
            errors.append(f"invalid manifest {artifact_namespace.value}: {message}")
            namespace_reports[artifact_namespace.value] = {
                "manifest": str(manifest_path),
                "valid": False,
                "works": 0,
                "source_records": 0,
                "artifacts": 0,
                "integrity_failures": [],
            }
            artifacts_by_namespace[artifact_namespace] = {}
            continue

        artifacts = {
            item.artifact_id: _artifact_integrity(workspace, directory, item)
            for item in manifest.artifacts
        }
        failures = [item for item in artifacts.values() if item["integrity"] != "ok"]
        errors.extend(
            f"{artifact_namespace.value}/{item['artifact_id']}: {item['error']}"
            for item in failures
        )
        artifacts_by_namespace[artifact_namespace] = artifacts
        namespace_reports[artifact_namespace.value] = {
            "manifest": str(manifest_path),
            "subject_paper_id": manifest.subject_paper_id,
            "valid": True,
            "works": len(manifest.works),
            "source_records": len(manifest.source_records),
            "artifacts": len(manifest.artifacts),
            "integrity_failures": failures,
        }

    research_ids = set(artifacts_by_namespace[ArtifactNamespace.RESEARCH_REFERENCE])
    subject_ids = set(artifacts_by_namespace[ArtifactNamespace.SUBJECT_REFERENCE])
    artifact_id_collisions = sorted(research_ids & subject_ids)
    if artifact_id_collisions:
        warnings.append(
            "cross-namespace artifact_id collisions are valid and require namespace-aware access"
        )

    address_lookup = None
    if selected_namespace is not None and artifact_id is not None:
        artifact = artifacts_by_namespace[selected_namespace].get(artifact_id)
        address_lookup = {
            "namespace": selected_namespace.value,
            "artifact_id": artifact_id,
            "found": artifact is not None,
            "artifact": artifact,
        }
        if artifact is None:
            errors.append(
                f"unknown artifact address: {selected_namespace.value}/{artifact_id}"
            )

    runtime_root = workspace / "runtime"
    reader_pattern = (
        f"{run_id}/tools/*_reader.json"
        if run_id is not None
        else "*/tools/*_reader.json"
    )
    reader_calls: list[dict[str, Any]] = []
    for path in sorted(runtime_root.glob(reader_pattern)):
        payload = _load_json(path, errors)
        if not isinstance(payload, dict):
            continue
        resolved = payload.get("resolved_arguments") or {}
        result = _read_result(payload)
        request_namespace = resolved.get("namespace")
        result_namespace = result.get("namespace")
        call = {
            "path": str(path.relative_to(workspace)),
            "execution_status": payload.get("execution_status"),
            "artifact_id": resolved.get("artifact_id"),
            "request_namespace": request_namespace,
            "result_namespace": result_namespace,
            "namespace_match": request_namespace == result_namespace,
        }
        reader_calls.append(call)
        if payload.get("execution_status") == "SUCCESS":
            if request_namespace != result_namespace:
                errors.append(f"Reader namespace mismatch: {call['path']}")
            try:
                resolved_namespace = ArtifactNamespace(request_namespace)
            except ValueError:
                errors.append(f"Reader has invalid namespace: {call['path']}")
            else:
                if resolved.get("artifact_id") not in artifacts_by_namespace[resolved_namespace]:
                    errors.append(f"Reader address missing from manifest: {call['path']}")

    evidence_bindings: list[dict[str, Any]] = []
    for path in sorted((workspace / "research-runs").glob("**/attempt-*.json")):
        payload = _load_json(path, errors)
        if not isinstance(payload, dict):
            continue
        for evidence in payload.get("evidence", []):
            if not isinstance(evidence, dict):
                continue
            provenance = evidence.get("provenance") or {}
            evidence_namespace = provenance.get("artifact_namespace")
            binding = {
                "path": str(path.relative_to(workspace)),
                "evidence_id": evidence.get("evidence_id"),
                "artifact_id": evidence.get("artifact_id"),
                "artifact_namespace": evidence_namespace,
                "resolved": False,
            }
            try:
                resolved_namespace = ArtifactNamespace(evidence_namespace)
            except ValueError:
                errors.append(
                    f"Evidence missing or invalid artifact_namespace: {binding['evidence_id']}"
                )
            else:
                binding["resolved"] = (
                    evidence.get("artifact_id")
                    in artifacts_by_namespace[resolved_namespace]
                )
                if not binding["resolved"]:
                    errors.append(
                        f"Evidence address missing from manifest: {binding['evidence_id']}"
                    )
            evidence_bindings.append(binding)

    unique_errors = list(dict.fromkeys(errors))
    return {
        "workspace": str(workspace),
        "run_id": run_id,
        "ok": not unique_errors,
        "namespaces": namespace_reports,
        "artifact_id_collisions": artifact_id_collisions,
        "address_lookup": address_lookup,
        "reader_calls": reader_calls,
        "evidence_bindings": evidence_bindings,
        "errors": unique_errors,
        "warnings": list(dict.fromkeys(warnings)),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("workspace", type=Path, help="outputs/<paper_id> 目录")
    parser.add_argument(
        "--namespace",
        choices=[item.value for item in ArtifactNamespace],
        help="与 --artifact-id 一起检查一个完整 Artifact 地址",
    )
    parser.add_argument("--artifact-id")
    parser.add_argument("--run-id", help="只检查指定 Runtime run 的 Reader 调用")
    parser.add_argument("--compact", action="store_true", help="输出单行 JSON")
    args = parser.parse_args()
    try:
        report = inspect_workspace(
            args.workspace,
            namespace=args.namespace,
            artifact_id=args.artifact_id,
            run_id=args.run_id,
        )
    except ValueError as exc:
        parser.error(str(exc))
    print(
        json.dumps(
            report,
            ensure_ascii=False,
            indent=None if args.compact else 2,
            sort_keys=True,
        )
    )
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
