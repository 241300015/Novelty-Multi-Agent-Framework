"""Runtime adapters for the established read-only workspace diagnostics."""

from __future__ import annotations

from typing import Any

from .contracts import RuntimeDiagnosticContext, diagnostic_status


def _runtime_result(
    name: str,
    context: RuntimeDiagnosticContext,
    report: dict[str, Any],
) -> dict[str, Any]:
    status = diagnostic_status(report)
    namespaces = report.get("namespaces")
    if (
        status == "WARNING"
        and isinstance(namespaces, dict)
        and namespaces
        and not any(item.get("valid") for item in namespaces.values())
    ):
        status = "INCOMPLETE"
    counts = report.get("counts", {})
    if not isinstance(counts, dict) or not counts:
        namespaces = report.get("namespaces", {})
        counts = {
            "reader_calls": len(report.get("reader_calls", [])),
            "evidence_bindings": len(report.get("evidence_bindings", [])),
            "namespace_integrity_failures": sum(
                len(item.get("integrity_failures", []))
                for item in namespaces.values()
            )
            if isinstance(namespaces, dict)
            else 0,
            "errors": len(report.get("errors", [])),
            "warnings": len(report.get("warnings", [])),
        }
    return {
        "diagnostic_name": name,
        "schema_version": "1.0",
        "status": status,
        "scope": {"paper_id": context.paper_id, "run_id": context.run_id},
        "counts": counts,
        "classification_counts": {},
        "verdict": {
            "primary_code": None,
            "summary": {
                "OK": "完整性检查通过",
                "WARNING": "完整性检查存在警告",
                "ERROR": "完整性检查发现错误",
                "INCOMPLETE": "业务产物尚不完整",
            }[status],
        },
        "report": report,
        "findings": [],
        "errors": report.get("errors", []),
        "warnings": report.get("warnings", []),
    }


class ReferenceNamespaceDiagnostic:
    name = "reference_namespace"
    schema_version = "1.0"

    def inspect(self, context: RuntimeDiagnosticContext) -> dict[str, Any]:
        from scripts.reference_namespace_diagnostics import inspect_workspace

        return _runtime_result(
            self.name,
            context,
            inspect_workspace(context.workspace, run_id=context.run_id),
        )


class ReviewerDiagnostic:
    name = "reviewer"
    schema_version = "1.0"

    def inspect(self, context: RuntimeDiagnosticContext) -> dict[str, Any]:
        from scripts.reviewer_diagnostics import inspect_workspace

        return _runtime_result(
            self.name,
            context,
            inspect_workspace(context.workspace, run_id=context.run_id),
        )


__all__ = ["ReferenceNamespaceDiagnostic", "ReviewerDiagnostic"]
