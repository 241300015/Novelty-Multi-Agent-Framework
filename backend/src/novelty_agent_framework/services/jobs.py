"""Web V1 使用的任务状态存储和公开响应契约。"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from threading import RLock
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field


class RunStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class RunStage(StrEnum):
    PARSE_PAPER = "parse_paper"
    EXTRACT_POINTS = "extract_points"
    PLAN_RESEARCH = "plan_research"
    RESEARCH = "research"
    VALIDATE_EVIDENCE = "validate_evidence"
    RENDER_REPORT = "render_report"


class CorrectionProgress(BaseModel):
    model_config = ConfigDict(extra="forbid")

    counts: dict[str, int] = Field(default_factory=dict)
    unresolved_correction_ids: list[str] = Field(default_factory=list)
    event_count: int = Field(default=0, ge=0)


class RunProgress(BaseModel):
    model_config = ConfigDict(extra="forbid")

    stage: RunStage
    round: int | None = Field(default=None, ge=1)
    corrections: CorrectionProgress | None = None


class RunError(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str = Field(min_length=1)
    message: str = Field(min_length=1)
    retryable: bool = False


class RunReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    available_formats: list[Literal["md", "pdf"]]
    preview_url: str
    downloads: dict[Literal["md", "pdf"], str]
    corrections_url: str | None = None


class RunSnapshot(BaseModel):
    """前端轮询任务状态时使用的统一响应。"""

    model_config = ConfigDict(extra="forbid")

    task_id: str = Field(min_length=1)
    status: RunStatus
    created_at: datetime
    updated_at: datetime
    result: dict[str, Any] | None = None
    error: str | RunError | None = None
    progress: RunProgress | None = None
    report: RunReport | None = None
    model_approval: dict[str, str] | None = None


class InMemoryRunStore:
    """单机首版任务存储；生产多实例部署应替换为共享持久化存储。"""

    def __init__(self) -> None:
        self._runs: dict[str, RunSnapshot] = {}
        self._report_paths: dict[str, Path] = {}
        self._lock = RLock()

    def create(self) -> RunSnapshot:
        now = datetime.now(UTC)
        snapshot = RunSnapshot(
            task_id=uuid4().hex,
            status=RunStatus.QUEUED,
            created_at=now,
            updated_at=now,
        )
        with self._lock:
            self._runs[snapshot.task_id] = snapshot
        return snapshot.model_copy(deep=True)

    def mark_running(self, task_id: str, *, stage: RunStage | None = None) -> RunSnapshot:
        progress = RunProgress(stage=stage) if stage is not None else None
        return self._update(
            task_id,
            status=RunStatus.RUNNING,
            progress=progress,
            result=None,
            error=None,
        )

    def mark_progress(
        self,
        task_id: str,
        stage: RunStage,
        *,
        round: int | None = None,
    ) -> RunSnapshot:
        with self._lock:
            current = self._runs.get(task_id)
            if current is None:
                raise KeyError(task_id)
            previous = current.progress
            effective_round = max(
                (value for value in (previous.round if previous else None, round) if value),
                default=None,
            )
            return self._update_locked(
                task_id,
                progress=RunProgress(stage=stage, round=effective_round),
            )

    def mark_succeeded(
        self,
        task_id: str,
        result: dict[str, Any],
        *,
        report: RunReport | None = None,
        report_path: Path | None = None,
    ) -> RunSnapshot:
        with self._lock:
            if report_path is not None:
                self._report_paths[task_id] = report_path
            return self._update_locked(
                task_id,
                status=RunStatus.SUCCEEDED,
                result=result,
                report=report,
                error=None,
            )

    def mark_failed(self, task_id: str, error: RunError) -> RunSnapshot:
        return self._update(
            task_id,
            status=RunStatus.FAILED,
            result=None,
            report=None,
            error=error,
        )

    def get(self, task_id: str) -> RunSnapshot | None:
        with self._lock:
            snapshot = self._runs.get(task_id)
            return snapshot.model_copy(deep=True) if snapshot else None

    def get_report_path(self, task_id: str) -> Path | None:
        with self._lock:
            return self._report_paths.get(task_id)

    def set_model_approval(self, task_id: str, request: dict[str, str] | None) -> RunSnapshot:
        return self._update(task_id, model_approval=request)

    def _update(self, task_id: str, **changes: Any) -> RunSnapshot:
        with self._lock:
            return self._update_locked(task_id, **changes)

    def _update_locked(self, task_id: str, **changes: Any) -> RunSnapshot:
        current = self._runs.get(task_id)
        if current is None:
            raise KeyError(task_id)
        updated = current.model_copy(
            update={**changes, "updated_at": datetime.now(UTC)},
            deep=True,
        )
        self._runs[task_id] = updated
        return updated.model_copy(deep=True)
