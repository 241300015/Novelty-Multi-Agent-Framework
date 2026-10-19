"""Stable contracts for read-only Runtime Debug diagnostics."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol


@dataclass(frozen=True)
class RuntimeDiagnosticContext:
    paper_id: str
    run_id: str
    workspace: Path
    run_dir: Path
    terminal_status: str


class RuntimeDiagnostic(Protocol):
    name: str
    schema_version: str

    def inspect(self, context: RuntimeDiagnosticContext) -> dict[str, Any]: ...


def diagnostic_status(report: dict[str, Any]) -> str:
    """Map a workspace report to the Runtime Debug status vocabulary."""

    errors = [str(item) for item in report.get("errors", [])]
    if errors and all(item.startswith("missing file:") for item in errors):
        return "INCOMPLETE"
    if errors:
        return "ERROR"
    if report.get("warnings"):
        return "WARNING"
    return "OK"


__all__ = ["RuntimeDiagnostic", "RuntimeDiagnosticContext", "diagnostic_status"]
