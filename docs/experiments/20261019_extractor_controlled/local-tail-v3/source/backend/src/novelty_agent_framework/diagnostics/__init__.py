"""Zero-model, read-only Runtime Debug diagnostics."""

from .contracts import RuntimeDiagnostic, RuntimeDiagnosticContext
from .reader_failures import ReaderFailureDiagnostic, inspect_reader_failures
from .workspace_diagnostics import ReferenceNamespaceDiagnostic, ReviewerDiagnostic

DEFAULT_RUNTIME_DIAGNOSTICS = (
    ReaderFailureDiagnostic(),
    ReferenceNamespaceDiagnostic(),
    ReviewerDiagnostic(),
)

__all__ = [
    "DEFAULT_RUNTIME_DIAGNOSTICS",
    "ReaderFailureDiagnostic",
    "ReferenceNamespaceDiagnostic",
    "ReviewerDiagnostic",
    "RuntimeDiagnostic",
    "RuntimeDiagnosticContext",
    "inspect_reader_failures",
]
