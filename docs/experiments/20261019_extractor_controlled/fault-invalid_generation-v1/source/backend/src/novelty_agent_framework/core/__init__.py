"""核心运行时能力；Harness 使用惰性导入以避免与 tools 形成导入环。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .errors import WorkflowExecutionError
from .runtime_artifacts import (
    RuntimeArtifactManager,
    RuntimeDebugConfig,
    StageHandle,
    ToolCallHandle,
    current_runtime_artifacts,
)
from .run_identity import display_path, file_run_identity, sha256_file
from .report_binding import bind_reviews_to_report

if TYPE_CHECKING:
    from .tool_call_harness import (
        ToolCallHarness,
        ToolCallHarnessConfig,
        ToolCallHarnessError,
        ToolCallHarnessResult,
    )

_HARNESS_EXPORTS = (
    "ToolCallHarness",
    "ToolCallHarnessConfig",
    "ToolCallHarnessError",
    "ToolCallHarnessResult",
)

__all__ = [
    "WorkflowExecutionError",
    "RuntimeArtifactManager",
    "RuntimeDebugConfig",
    "display_path",
    "file_run_identity",
    "sha256_file",
    "StageHandle",
    "ToolCallHandle",
    "current_runtime_artifacts",
    "bind_reviews_to_report",
    *_HARNESS_EXPORTS,
]


def __getattr__(name: str) -> Any:
    if name not in _HARNESS_EXPORTS:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from . import tool_call_harness

    value = getattr(tool_call_harness, name)
    globals()[name] = value
    return value
