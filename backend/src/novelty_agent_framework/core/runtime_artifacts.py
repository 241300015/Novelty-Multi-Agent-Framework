"""Incremental, local runtime-debug artifacts owned by the Harness layer.

The recorder is deliberately observational: it persists facts at run, stage and
tool boundaries, but does not attempt root-cause diagnosis or replay.
"""

from __future__ import annotations

import contextvars
import dataclasses
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import tempfile
import threading
import time
import traceback as traceback_module
import uuid
from datetime import date, datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Mapping, Sequence

from backend.env import (
    ModelCallBudgetExceeded,
    ModelCallEvent,
    reset_model_call_observer,
    set_model_call_observer,
)

from ..diagnostics import (
    DEFAULT_RUNTIME_DIAGNOSTICS,
    RuntimeDiagnostic,
    RuntimeDiagnosticContext,
)
from ..diagnostics.llm_usage import DEFAULT_PRICING_PATH, LlmPricingCatalog

try:
    from pydantic import BaseModel
except ImportError:  # pragma: no cover - the application depends on pydantic
    BaseModel = ()  # type: ignore[assignment,misc]


RUN_STATUSES = {"RUNNING", "SUCCESS", "FAILED", "INTERRUPTED"}


class ProviderPhysicalBudgetExceeded(RuntimeError):
    """A run would exceed its physical database-provider request cap."""


_REDACTED = "***REDACTED***"
_SENSITIVE_KEYS = {
    "apikey",
    "token",
    "accesstoken",
    "refreshtoken",
    "bearertoken",
    "secret",
    "clientsecret",
    "password",
    "authorization",
    "cookie",
    "setcookie",
}
_SENSITIVE_TEXT = re.compile(
    r"(?i)\b(api[_-]?key|token|secret|password|authorization|cookie)"
    r"\s*[:=]\s*([^\s,;]+)"
)
_SENSITIVE_URL_VALUE = re.compile(
    r"(?i)([?&][^=&#\s]*(?:signature|credential|token|secret|key|password|auth)[^=&#\s]*=)[^&#\s]+"
)
_URL_USERINFO = re.compile(r"(?i)(https?://)[^/@\s]+:[^/@\s]+@")

_current_run: contextvars.ContextVar[RuntimeArtifactManager | None] = (
    contextvars.ContextVar("novelty_runtime_artifact_manager", default=None)
)
_current_stage: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "novelty_runtime_stage", default=None
)
_current_stage_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "novelty_runtime_stage_id", default=None
)
_current_scope: contextvars.ContextVar[dict[str, Any]] = contextvars.ContextVar(
    "novelty_runtime_scope", default={}
)


@dataclasses.dataclass(frozen=True)
class RuntimeDebugConfig:
    """Runtime-debug storage settings. Debug recording is on by default."""

    enabled: bool = True
    output_root: Path = Path("outputs")
    archive_root: Path = Path("docs/experiments/runtime")
    max_inline_bytes: int = 256_000
    max_physical_provider_requests: int = 48
    max_model_calls: int = 80
    llm_pricing_path: Path = DEFAULT_PRICING_PATH

    def __post_init__(self) -> None:
        if self.max_inline_bytes < 1:
            raise ValueError("max_inline_bytes must be positive")
        if self.max_physical_provider_requests < 1:
            raise ValueError("max_physical_provider_requests must be positive")
        if self.max_model_calls < 1:
            raise ValueError("max_model_calls must be positive")


@dataclasses.dataclass(frozen=True)
class StageHandle:
    stage_id: str
    stage_name: str
    directory: Path | None
    started_at: datetime
    monotonic_started: float
    context_token: contextvars.Token[str | None] | None = None
    stage_id_token: contextvars.Token[str | None] | None = None
    scope_token: contextvars.Token[dict[str, Any]] | None = None


@dataclasses.dataclass(frozen=True)
class ToolCallHandle:
    tool_call_id: str
    tool_name: str
    stage_name: str | None
    path: Path | None
    started_at: datetime
    monotonic_started: float
    agent_tool_call_id: str | None = None


class RuntimeArtifactManager:
    """Own one run's paths, identifiers, redaction and incremental writes."""

    def __init__(
        self,
        paper_id: str,
        *,
        config: RuntimeDebugConfig | None = None,
        run_id: str | None = None,
        run_identity: Mapping[str, Any] | None = None,
        runtime_config: Mapping[str, Any] | None = None,
        model_provider: str | None = None,
        model_name: str | None = None,
        enabled_tools: list[str] | tuple[str, ...] = (),
        stage_names: list[str] | tuple[str, ...] = (),
        diagnostics: Sequence[RuntimeDiagnostic] | None = None,
    ) -> None:
        self.config = config or RuntimeDebugConfig()
        self.paper_id = paper_id
        self.run_id = run_id or _new_run_id()
        self.run_identity = dict(run_identity or {})
        self.runtime_config = dict(runtime_config or {})
        self.model_provider = model_provider
        self.model_name = model_name
        self.enabled_tools = list(enabled_tools)
        self.stage_names = list(dict.fromkeys(stage_names))
        self.diagnostics = tuple(
            DEFAULT_RUNTIME_DIAGNOSTICS if diagnostics is None else diagnostics
        )
        self.started_at = _now()
        self._monotonic_started = time.monotonic()
        self._lock = threading.RLock()
        self._stage_counter = 0
        self._tool_counter = 0
        self._llm_call_counter = 0
        self._llm_call_paths: dict[str, Path] = {}
        self._archived_run_dir: Path | None = None
        self._finished_summary: dict[str, Any] | None = None
        self._error_counter = 0
        self._provider_request_counter = 0
        self._provider_dispatch_count = 0
        self._planner_event_counter = 0
        self._retrieval_event_counter = 0
        self._stage_records: list[dict[str, Any]] = []
        self._tool_records: list[dict[str, Any]] = []
        self._llm_call_records: list[dict[str, Any]] = []
        self._error_records: list[dict[str, Any]] = []
        self._provider_request_records: list[dict[str, Any]] = []
        self._diagnostic_results: list[dict[str, Any]] = []
        self._outcome: dict[str, Any] | None = None
        self._activation_token: (
            contextvars.Token[RuntimeArtifactManager | None] | None
        ) = None
        self._model_observer_token: contextvars.Token[Any] | None = None
        self._pricing_catalog: LlmPricingCatalog | None = None
        self.run_dir: Path | None = None
        if self.config.enabled:
            safe_paper_id = _safe_segment(paper_id)
            self.run_dir = (
                Path(self.config.output_root)
                / safe_paper_id
                / "runtime"
                / _safe_segment(self.run_id)
            )
            for child in ("stages", "tools", "llm_calls", "errors", "provider_requests",
                          "planner_events", "retrieval_events", "provider_budget",
                          "blobs", "config"):
                (self.run_dir / child).mkdir(parents=True, exist_ok=False)
            if self.config.llm_pricing_path.is_file():
                shutil.copy2(self.config.llm_pricing_path,
                             self.run_dir / "config" / "llm_pricing.json")
            try:
                self._pricing_catalog = LlmPricingCatalog.load(
                    self.config.llm_pricing_path
                )
            except (OSError, ValueError, json.JSONDecodeError):
                # Usage tracking remains useful even when a local price table is bad.
                self._pricing_catalog = None
            self._write_manifest("RUNNING")

    def __enter__(self) -> RuntimeArtifactManager:
        self.activate()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.deactivate()

    def activate(self) -> None:
        if self._activation_token is not None:
            raise RuntimeError("runtime artifact manager is already active")
        self._activation_token = _current_run.set(self)
        self._model_observer_token = set_model_call_observer(self.record_model_call)

    def deactivate(self) -> None:
        if self._model_observer_token is not None:
            reset_model_call_observer(self._model_observer_token)
            self._model_observer_token = None
        if self._activation_token is not None:
            _current_run.reset(self._activation_token)
            self._activation_token = None

    def record_outcome(self, outcome: Mapping[str, Any]) -> None:
        """Attach a compact, structured business outcome to the run summary."""

        self._outcome = dict(outcome)

    def record_diagnostic_artifact(self, name: str, payload: Mapping[str, Any]) -> None:
        """Store a small, redacted sidecar beside the existing runtime diagnostics."""
        if not self.config.enabled or self.run_dir is None:
            return
        if not re.fullmatch(r"[a-z][a-z0-9_]*\.json", name):
            raise ValueError("invalid diagnostic artifact name")
        with self._lock:
            self._write_json(self.run_dir / "diagnostics" / name, dict(payload))

    def record_provider_request(self, event: Mapping[str, Any]) -> None:
        """Persist a provider scheduler event without affecting business flow."""

        if not self.config.enabled or self.run_dir is None:
            return
        with self._lock:
            self._provider_request_counter += 1
            record = {
                "provider_event_id": f"provider_{self._provider_request_counter:04d}",
                "run_id": self.run_id,
                "stage_name": _current_stage.get(),
                "parent_stage_id": _current_stage_id.get(),
                "scope": _current_scope.get(),
                "recorded_at": _iso(_now()),
                **dict(event),
            }
            self._provider_request_records.append(record)
            self._write_json(
                self.run_dir
                / "provider_requests"
                / f"{self._provider_request_counter:04d}_{_safe_segment(str(event.get('operation', 'request')))}.json",
                record,
            )

    def reserve_provider_request(self, *, provider: str, operation: str) -> int:
        """Reserve one physical HTTP dispatch before any database network I/O."""
        if not self.config.enabled or self.run_dir is None:
            return 0
        with self._lock:
            if self._provider_dispatch_count >= self.config.max_physical_provider_requests:
                raise ProviderPhysicalBudgetExceeded(
                    "retrieval_incomplete_budget: physical provider request cap reached"
                )
            next_count = self._provider_dispatch_count + 1
            self._write_json(self.run_dir / "provider_budget" / f"{next_count:04d}.json", {
                "run_id": self.run_id, "dispatch_index": next_count,
                "provider": provider, "operation": operation,
                "scope": _current_scope.get(), "parent_stage_id": _current_stage_id.get(),
                "reserved_at": _iso(_now()),
                "max_physical_provider_requests": self.config.max_physical_provider_requests,
            })
            self._provider_dispatch_count = next_count
            return next_count

    def record_planner_event(self, event: Mapping[str, Any]) -> None:
        """Persist draft validation and compilation steps beside model calls."""
        if not self.config.enabled or self.run_dir is None:
            return
        with self._lock:
            self._planner_event_counter += 1
            scope = _current_scope.get()
            parent = next((item["llm_call_id"] for item in reversed(self._llm_call_records)
                           if item.get("scope") == scope
                           and item.get("parent_stage_id") == _current_stage_id.get()), None)
            record = {
                "planner_event_id": f"planner_{self._planner_event_counter:04d}",
                "run_id": self.run_id,
                "parent_stage_id": _current_stage_id.get(),
                "parent_llm_call_id": parent,
                "scope": scope,
                "recorded_at": _iso(_now()),
                **dict(event),
            }
            path = self.run_dir / "planner_events" / f"{self._planner_event_counter:04d}.json"
            self._write_json(path, record)

    def record_retrieval_event(self, event: Mapping[str, Any]) -> None:
        """Persist provider-boundary objects and candidate-selection decisions."""
        if not self.config.enabled or self.run_dir is None:
            return
        with self._lock:
            self._retrieval_event_counter += 1
            record = {
                "retrieval_event_id": f"retrieval_{self._retrieval_event_counter:04d}",
                "run_id": self.run_id,
                "parent_stage_id": _current_stage_id.get(),
                "scope": _current_scope.get(),
                "recorded_at": _iso(_now()),
                **dict(event),
            }
            path = self.run_dir / "retrieval_events" / f"{self._retrieval_event_counter:04d}.json"
            self._write_json(path, record)

    def record_reviewer_event(self, event: Mapping[str, Any]) -> None:
        """Persist Reviewer materials, registered reads, shadows and summary inputs."""
        if not self.config.enabled or self.run_dir is None:
            return
        with self._lock:
            directory = self.run_dir / "reviewer_events"
            directory.mkdir(parents=True, exist_ok=True)
            index = len(list(directory.glob("*.json"))) + 1
            record = {"reviewer_event_id": f"reviewer_{index:04d}", "run_id": self.run_id,
                      "parent_stage_id": _current_stage_id.get(), "scope": _current_scope.get(),
                      "recorded_at": _iso(_now()), **dict(event)}
            self._write_json(directory / f"{index:04d}.json", record)

    def record_model_call(self, event: ModelCallEvent) -> None:
        """Persist one model call and its normalized token/cost accounting."""

        if not self.config.enabled or self.run_dir is None:
            return
        reported_usage = event.provider_usage
        if reported_usage is None and event.response is not None:
            reported_usage = event.response.usage
        with self._lock:
            call_key = event.call_id or uuid.uuid4().hex
            existing = next((item for item in self._llm_call_records
                             if item.get("client_call_id") == call_key), None)
            # Cancellation/error notifications contain no consumption payload.
            # They must never erase usage already observed for this call ID.
            usage = (dict(reported_usage) if isinstance(reported_usage, Mapping) and reported_usage
                     else dict(existing.get("provider_usage") or {}) if existing else {})
            occurred_at = (datetime.fromisoformat(existing["started_at"])
                           if existing else event.started_at)
            accounting = self._model_accounting(event.model, usage, occurred_at)
            milestone_phases = {"TRANSPORT_INVOKED", "RESPONSE_HEADERS",
                                "RESPONSE_BODY_COMPLETE", "RESPONSE_PARSED", "CONTEXT_ADMISSION",
                                "CONTEXT_MEASUREMENT_REQUEST", "CONTEXT_MEASUREMENT_RESPONSE",
                                "CONTEXT_MEASUREMENT_FAILED", "CONTEXT_MEASUREMENT_VERIFIED"}
            if existing is not None and event.phase in milestone_phases:
                existing.setdefault("timeline", []).append({
                    "phase": event.phase, "at": _iso(event.started_at),
                    **dict(event.details)})
                existing.update(provider_usage=usage, **accounting)
                if event.phase == "RESPONSE_PARSED" and event.details.get("response_id") is not None:
                    existing["request_id"] = event.details["response_id"]
                if event.phase == "CONTEXT_ADMISSION":
                    existing["context_admission"] = dict(event.details)
                elif event.phase == "CONTEXT_MEASUREMENT_VERIFIED":
                    existing["context_measurement_verification"] = dict(event.details)
                self._write_model_record(call_key, existing)
                return
            if existing is not None and existing["status"] == "CANCELLED" and event.phase != "CANCELLED":
                existing["late_completion"] = {
                    "at": _iso(_now()), "status": "FAILED" if event.error else "SUCCESS",
                    "response_id": (event.response.raw.get("id") if event.response
                                    and isinstance(event.response.raw, Mapping) else None),
                    "usage": dict(usage), "billing": accounting["billing"],
                    "error": (f"{type(event.error).__name__}: {event.error}" if event.error else None),
                }
                existing.update(provider_usage=usage, **accounting)
                if event.response is not None and isinstance(event.response.raw, Mapping):
                    existing["request_id"] = event.response.raw.get("id") or existing.get("request_id")
                existing["transport_inflight_unknown"] = False
                self._write_model_record(call_key, existing)
                return
            if (existing is None and event.phase == "START"
                    and self._llm_call_counter >= self.config.max_model_calls):
                raise ModelCallBudgetExceeded("model_call_budget_exhausted")
            path = self._llm_call_paths.get(call_key)
            if path is None:
                self._llm_call_counter += 1
                path = self.run_dir / "llm_calls" / (
                    f"{self._llm_call_counter:04d}_{_safe_segment(event.alias)}.json"
                )
                self._llm_call_paths[call_key] = path
            call_id = f"llm_{int(path.name.split('_', 1)[0]):04d}"
            record = {
                "llm_call_id": call_id,
                "client_call_id": call_key,
                "run_id": self.run_id,
                "stage_name": _current_stage.get(),
                "parent_stage_id": _current_stage_id.get(),
                "scope": _current_scope.get(),
                "alias": event.alias,
                "provider": event.provider,
                "model": event.model,
                "started_at": _iso(event.started_at),
                "duration_ms": event.duration_ms,
                "message_count": event.message_count,
                "status": ("RUNNING" if event.phase == "START" else
                           "CANCELLED" if event.phase == "CANCELLED" else
                           "FAILED" if event.error is not None else "SUCCESS"),
                "request_payload": event.request_payload,
                "request_options": event.request_options,
                "response": ({"content": event.response.content,
                              "tool_calls": event.response.tool_calls}
                             if event.response is not None else None),
                "finish_reason": (
                    event.response.raw["choices"][0].get("finish_reason")
                    if event.response is not None
                    and isinstance(event.response.raw, Mapping)
                    and isinstance(event.response.raw.get("choices"), list)
                    and event.response.raw["choices"]
                    and isinstance(event.response.raw["choices"][0], Mapping)
                    else None
                ),
                "request_id": (
                    event.response.raw.get("id")
                    if event.response is not None
                    and isinstance(event.response.raw, Mapping)
                    else existing.get("request_id") if existing else None
                ),
                **accounting,
                "provider_usage": dict(usage),
                "error": (
                    {"type": type(event.error).__name__, "message": str(event.error),
                     **({"code": event.error.code} if hasattr(event.error, "code") else {})}
                    if event.error is not None
                    else None
                ),
                "timeline": existing.get("timeline", []) if existing else [],
                "context_admission": existing.get("context_admission") if existing else None,
                "context_measurement_verification": existing.get("context_measurement_verification") if existing else None,
                "chat_transport_invoked": bool(existing and any(
                    item["phase"] == "TRANSPORT_INVOKED" for item in existing.get("timeline", []))),
                "context_measurement_requests": sum(
                    item["phase"] == "CONTEXT_MEASUREMENT_REQUEST"
                    for item in (existing.get("timeline", []) if existing else [])),
                "transport_inflight_unknown": (
                    event.phase == "CANCELLED" and existing is not None
                    and any(item["phase"] == "TRANSPORT_INVOKED"
                            for item in existing.get("timeline", []))
                    and not any(item["phase"] == "RESPONSE_PARSED"
                                for item in existing.get("timeline", []))
                ),
            }
            if existing is not None:
                for prior_detail in ("late_completion", "transport_completion_before_cancel"):
                    if prior_detail in existing:
                        record[prior_detail] = existing[prior_detail]
            if existing is not None and event.phase == "CANCELLED" and existing["status"] in {"SUCCESS", "FAILED"}:
                record["transport_completion_before_cancel"] = {
                    "status": existing["status"], "response": existing["response"],
                    "request_id": existing["request_id"], "usage": existing["provider_usage"],
                    "billing": existing["billing"], "error": existing["error"],
                }
            prior = next((i for i, item in enumerate(self._llm_call_records)
                          if item["client_call_id"] == call_key), None)
            if prior is None:
                self._llm_call_records.append(record)
            else:
                self._llm_call_records[prior] = record
            self._write_model_record(call_key, record)

    def _model_accounting(self, model: str, usage: Mapping[str, Any],
                          occurred_at: datetime) -> dict[str, Any]:
        if self._pricing_catalog is not None:
            return self._pricing_catalog.calculate(model, usage, occurred_at=occurred_at)
        from ..diagnostics.llm_usage import normalize_usage

        return {"tokens": normalize_usage(usage), "billing": {
            "status": "PRICING_UNAVAILABLE", "currency": "RMB", "amount": None,
            "unit_tokens": None, "rate_name": None, "rates_per_unit": None,
            "pricing_table": str(self.config.llm_pricing_path),
        }}

    def _write_model_record(self, call_key: str, record: Mapping[str, Any]) -> None:
        path = self._llm_call_paths[call_key]
        self._write_json(path, record)
        if self._archived_run_dir is not None:
            self._write_json(self._archived_run_dir / "llm_calls" / path.name, record)
        if self._finished_summary is not None:
            usage = _summarize_llm_usage(self._llm_call_records)
            if usage != self._finished_summary["llm_usage"]:
                # Reconcile only accounting. Run outcome, terminal time and
                # duration describe execution, not the arrival of a late bill.
                self._finished_summary = {**self._finished_summary,
                    "llm_usage": usage, "llm_usage_reconciled_at": _iso(_now())}
                for directory in (self.run_dir, self._archived_run_dir):
                    if directory is not None:
                        self._write_json(directory / "summary.json", self._finished_summary)
                        _atomic_write_text(directory / "summary.md", _render_summary(self._finished_summary))

    def start_stage(self, stage_name: str, stage_input: Any) -> StageHandle:
        started = _now()
        token = _current_stage.set(stage_name)
        scope_token = _current_scope.set(_scope_from_stage_input(stage_input))
        if not self.config.enabled:
            return StageHandle(
                "", stage_name, None, started, time.monotonic(), token,
                None, scope_token,
            )
        with self._lock:
            self._stage_counter += 1
            stage_id = f"stage_{self._stage_counter:04d}"
            stage_id_token = _current_stage_id.set(stage_id)
            directory = self.run_dir / "stages" / (
                f"{self._stage_counter:04d}_{_safe_segment(stage_name)}"
            )
            directory.mkdir(parents=False, exist_ok=False)
            debug_details = _stage_debug_details(
                stage_name,
                stage_input=stage_input,
                stage_output=None,
                runtime_config=self.runtime_config,
            )
            record = {
                "stage_id": stage_id,
                "stage_name": stage_name,
                "scope": _current_scope.get(),
                "parent_stage_id": stage_id_token.old_value if isinstance(stage_id_token.old_value, str) else None,
                "status": "RUNNING",
                "started_at": _iso(started),
                "finished_at": None,
                "duration": None,
                "duration_ms": None,
                "error": None,
                "validation_result": None,
                "directory": directory,
            }
            if debug_details is not None:
                record["debug_details"] = debug_details
                record["_stage_input"] = stage_input
            self._stage_records.append(record)
            self._write_json(directory / "input.json", stage_input)
            self._write_json(directory / "meta.json", _public_record(record))
            return StageHandle(
                stage_id, stage_name, directory, started, time.monotonic(), token,
                stage_id_token, scope_token,
            )

    def finish_stage(self, handle: StageHandle, stage_output: Any) -> None:
        if handle.stage_id_token is not None:
            _current_stage_id.reset(handle.stage_id_token)
        if handle.scope_token is not None:
            _current_scope.reset(handle.scope_token)
        if handle.context_token is not None:
            _current_stage.reset(handle.context_token)
        if not self.config.enabled or handle.directory is None:
            return
        finished = _now()
        validation_result = _stage_validation_result(
            handle.stage_name, stage_output
        )
        with self._lock:
            record = self._find_record(self._stage_records, "stage_id", handle.stage_id)
            stage_input = record.pop("_stage_input", None)
            debug_details = _stage_debug_details(
                handle.stage_name,
                stage_input=stage_input,
                stage_output=stage_output,
                runtime_config=self.runtime_config,
            )
            record.update(
                status="SUCCESS",
                finished_at=_iso(finished),
                duration=_elapsed_seconds(handle.monotonic_started),
                duration_ms=_elapsed_ms(handle.monotonic_started),
                validation_result=validation_result,
            )
            if debug_details is not None:
                record["debug_details"] = debug_details
            self._write_json(handle.directory / "output.json", stage_output)
            self._write_json(handle.directory / "meta.json", _public_record(record))

    def fail_stage(self, handle: StageHandle, exc: BaseException) -> None:
        if handle.stage_id_token is not None:
            _current_stage_id.reset(handle.stage_id_token)
        if handle.scope_token is not None:
            _current_scope.reset(handle.scope_token)
        if handle.context_token is not None:
            _current_stage.reset(handle.context_token)
        if not self.config.enabled or handle.directory is None:
            return
        finished = _now()
        error = self._error_payload(exc, stage=handle.stage_name)
        with self._lock:
            record = self._find_record(self._stage_records, "stage_id", handle.stage_id)
            record.pop("_stage_input", None)
            record.update(
                status="FAILED",
                finished_at=_iso(finished),
                duration=_elapsed_seconds(handle.monotonic_started),
                duration_ms=_elapsed_ms(handle.monotonic_started),
                error=error,
            )
            self._write_json(handle.directory / "meta.json", _public_record(record))
            self._persist_error(error)

    def start_tool_call(
        self,
        tool_name: str,
        *,
        agent_arguments: Any,
        resolved_arguments: Any,
        agent_tool_call_id: str | None = None,
        stage_name: str | None = None,
    ) -> ToolCallHandle:
        started = _now()
        stage = stage_name if stage_name is not None else _current_stage.get()
        if not self.config.enabled:
            return ToolCallHandle(
                "", tool_name, stage, None, started, time.monotonic(), agent_tool_call_id
            )
        with self._lock:
            self._tool_counter += 1
            tool_call_id = f"tool_{self._tool_counter:04d}"
            path = self.run_dir / "tools" / (
                f"{self._tool_counter:04d}_{_safe_segment(tool_name)}.json"
            )
            record = {
                "tool_name": tool_name,
                "tool_call_id": tool_call_id,
                "run_id": self.run_id,
                "parent_stage_id": _current_stage_id.get(),
                "scope": _current_scope.get(),
                "agent_tool_call_id": agent_tool_call_id,
                "parent_llm_call_id": self._model_parent_for_tool_call(agent_tool_call_id),
                "stage_name": stage,
                "started_at": _iso(started),
                "finished_at": None,
                "duration": None,
                "duration_ms": None,
                "execution_status": "RUNNING",
                "failure_phase": None,
                "business_status": None,
                "result_count": None,
                "agent_arguments": agent_arguments,
                "resolved_arguments": resolved_arguments,
                "raw_result": None,
                "normalized_result": None,
                "error": None,
                "path": path,
            }
            self._tool_records.append(record)
            self._write_json(path, _public_record(record))
            return ToolCallHandle(
                tool_call_id,
                tool_name,
                stage,
                path,
                started,
                time.monotonic(),
                agent_tool_call_id,
            )

    def _model_parent_for_tool_call(self, agent_tool_call_id: str | None) -> str | None:
        if not agent_tool_call_id:
            return None
        scope = _current_scope.get()
        for record in reversed(self._llm_call_records):
            if record.get("scope") != scope:
                continue
            response = record.get("response") or {}
            if any(getattr(call, "id", None) == agent_tool_call_id
                   for call in response.get("tool_calls", ())):
                return record["llm_call_id"]
        return None

    def finish_tool_call(
        self,
        handle: ToolCallHandle,
        *,
        raw_result: Any,
        normalized_result: Any,
        succeeded: bool = True,
        error: BaseException | str | Mapping[str, Any] | None = None,
        result_count: int | None = None,
        business_status: str | None = None,
        failure_phase: str | None = None,
    ) -> None:
        if not self.config.enabled or handle.path is None:
            return
        with self._lock:
            record = self._find_record(
                self._tool_records, "tool_call_id", handle.tool_call_id
            )
            error_payload = self._coerce_error(
                error, stage=handle.stage_name, tool_call_id=handle.tool_call_id
            )
            if result_count is None:
                result_count = _infer_result_count(normalized_result)
            if business_status is None:
                retrieval_status = normalized_result.get("retrieval_status") if isinstance(normalized_result, Mapping) else None
                read_status = (normalized_result or {}).get("read_status") if isinstance(normalized_result, dict) else None
                business_status = retrieval_status or (
                    "ARTIFACT_EOF" if read_status == "artifact_eof" else
                    "EMPTY" if succeeded and result_count == 0 else "NORMAL")
            record.update(
                finished_at=_iso(_now()),
                duration=_elapsed_seconds(handle.monotonic_started),
                duration_ms=_elapsed_ms(handle.monotonic_started),
                execution_status="SUCCESS" if succeeded else "FAILED",
                failure_phase=(
                    None
                    if succeeded
                    else failure_phase
                    or ("NORMALIZATION" if raw_result is not None and normalized_result is None
                        else "TOOL_EXECUTION")
                ),
                business_status=business_status,
                result_count=result_count,
                raw_result=raw_result,
                normalized_result=normalized_result,
                error=error_payload,
            )
            self._write_json(handle.path, _public_record(record))
            if error_payload is not None:
                self._persist_error(error_payload)

    def fail_tool_call(
        self,
        handle: ToolCallHandle,
        exc: BaseException,
        *,
        raw_result: Any = None,
        normalized_result: Any = None,
        failure_phase: str | None = None,
    ) -> None:
        self.finish_tool_call(
            handle,
            raw_result=raw_result,
            normalized_result=normalized_result,
            succeeded=False,
            error=exc,
            business_status="INVALID" if raw_result is not None else "NORMAL",
            failure_phase=failure_phase,
        )

    def finish_run(
        self,
        status: str,
        *,
        error: BaseException | None = None,
    ) -> tuple[Path | None, Path | None]:
        if status not in RUN_STATUSES - {"RUNNING"}:
            raise ValueError(f"invalid terminal run status {status!r}")
        if not self.config.enabled:
            return None, None
        with self._lock:
            if error is not None:
                self._persist_error(self._error_payload(error, stage=_current_stage.get()))
            finished = _now()
            self._write_not_run_stages()
            self._run_diagnostics(status)
            summary = _prepare_value(
                self._build_summary(status, finished),
                max_inline_bytes=self.config.max_inline_bytes,
            )
            summary_json = self.run_dir / "summary.json"
            summary_md = self.run_dir / "summary.md"
            self._write_json(summary_json, summary)
            _atomic_write_text(summary_md, _render_summary(summary))
            self._write_manifest(status, end_time=finished)
            archive = (
                Path(self.config.archive_root)
                / f"{_safe_segment(self.paper_id)}_{finished.date().isoformat()}"
                / _safe_segment(self.run_id)
            )
            archive.mkdir(parents=True, exist_ok=False)
            shutil.copytree(self.run_dir, archive, dirs_exist_ok=True)
            self._archived_run_dir = archive
            self._finished_summary = summary
            workspace = Path(self.config.output_root) / _safe_segment(self.paper_id)
            if workspace.is_dir():
                shutil.copytree(
                    workspace, archive / "workspace" / _safe_segment(self.paper_id),
                    ignore=shutil.ignore_patterns("runtime"),
                )
            self._write_json(archive / "archive-layout.json", {
                "runtime_root": ".",
                "workspace_root": f"workspace/{_safe_segment(self.paper_id)}",
                "capture_boundary": "finish_run",
            })
            return summary_json, archive

    def _run_diagnostics(self, terminal_status: str) -> None:
        if not self.diagnostics:
            return
        diagnostics_dir = self.run_dir / "diagnostics"
        diagnostics_dir.mkdir(parents=False, exist_ok=True)
        context = RuntimeDiagnosticContext(
            paper_id=self.paper_id,
            run_id=self.run_id,
            workspace=Path(self.config.output_root) / _safe_segment(self.paper_id),
            run_dir=self.run_dir,
            terminal_status=terminal_status,
        )
        for diagnostic in self.diagnostics:
            try:
                result = diagnostic.inspect(context)
                if not isinstance(result, dict):
                    raise TypeError("diagnostic result must be a dictionary")
            except Exception as exc:  # diagnostics must never replace business status
                result = {
                    "diagnostic_name": diagnostic.name,
                    "schema_version": diagnostic.schema_version,
                    "status": "ERROR",
                    "scope": {"paper_id": self.paper_id, "run_id": self.run_id},
                    "counts": {},
                    "classification_counts": {},
                    "verdict": {
                        "primary_code": None,
                        "summary": "诊断器执行失败",
                    },
                    "findings": [],
                    "errors": [f"{type(exc).__name__}: {exc}"],
                    "warnings": [],
                }
            prepared = _prepare_value(
                result, max_inline_bytes=self.config.max_inline_bytes
            )
            self._write_json(
                diagnostics_dir / f"{_safe_segment(diagnostic.name)}.json", prepared
            )
            self._diagnostic_results.append(prepared)

    def _write_not_run_stages(self) -> None:
        executed_names = {item["stage_name"] for item in self._stage_records}
        for stage_name in self.stage_names:
            if stage_name in executed_names:
                continue
            directory = self.run_dir / "stages" / f"not_run_{_safe_segment(stage_name)}"
            directory.mkdir(parents=False, exist_ok=True)
            self._write_json(
                directory / "meta.json",
                {
                    "stage_id": None,
                    "stage_name": stage_name,
                    "status": "NOT_RUN",
                    "started_at": None,
                    "finished_at": None,
                    "duration": None,
                    "duration_ms": None,
                    "error": None,
                    "validation_result": None,
                },
            )

    def _write_manifest(self, status: str, end_time: datetime | None = None) -> None:
        if status not in RUN_STATUSES:
            raise ValueError(f"invalid run status {status!r}")
        branch, commit = _git_identity()
        manifest = {
            "run_id": self.run_id,
            "paper_id": self.paper_id,
            "status": status,
            "start_time": _iso(self.started_at),
            "end_time": _iso(end_time) if end_time else None,
            "duration_ms": (
                _elapsed_ms(self._monotonic_started) if end_time else None
            ),
            "git_branch": branch,
            "git_commit": commit,
            "python_version": platform.python_version(),
            "os": platform.platform(),
            "runtime_debug_enabled": self.config.enabled,
            "max_physical_provider_requests": self.config.max_physical_provider_requests,
            "physical_provider_requests_reserved": self._provider_dispatch_count,
            "max_model_calls": self.config.max_model_calls,
            "llm_pricing_path": ("config/llm_pricing.json"
                                 if (self.run_dir / "config" / "llm_pricing.json").is_file()
                                 else None),
            "model_provider": self.model_provider,
            "model_name": self.model_name,
            "enabled_tools": self.enabled_tools,
            "entrypoint": self.run_identity.get("entrypoint"),
            "input_identity": self.run_identity.get("input_identity", {}),
            "runtime_diagnostics": [
                {"diagnostic_name": item.name, "schema_version": item.schema_version}
                for item in self.diagnostics
            ],
            "runtime_config": self.runtime_config,
        }
        self._write_json(self.run_dir / "manifest.json", manifest)

    def _build_summary(self, status: str, finished: datetime) -> dict[str, Any]:
        executed_names = {item["stage_name"] for item in self._stage_records}
        stages = [_public_record(item) for item in self._stage_records]
        stages.extend(
            {
                "stage_id": None,
                "stage_name": name,
                "status": "NOT_RUN",
                "started_at": None,
                "finished_at": None,
                "duration_ms": None,
                "duration": None,
                "error": None,
            }
            for name in self.stage_names
            if name not in executed_names
        )
        tool_stats: dict[str, dict[str, Any]] = {}
        for record in self._tool_records:
            stats = tool_stats.setdefault(
                record["tool_name"],
                {"tool_name": record["tool_name"], "calls": 0, "success": 0,
                 "failed": 0, "empty": 0},
            )
            stats["calls"] += 1
            if record["execution_status"] == "SUCCESS":
                stats["success"] += 1
            elif record["execution_status"] == "FAILED":
                stats["failed"] += 1
            if record["business_status"] in {"EMPTY", "ZERO_RESULT"}:
                stats["empty"] += 1
        completed = [item for item in self._stage_records if item["status"] == "SUCCESS"]
        integrity_gates = [
            {
                "stage_name": item["stage_name"],
                **item["validation_result"],
            }
            for item in self._stage_records
            if item.get("validation_result") is not None
        ]
        final_evidence_sufficiency_checks = []
        reviewer_information_adjudication_checks = []
        for index, item in enumerate(self._stage_records):
            details = item.get("debug_details")
            if not isinstance(details, Mapping):
                continue
            next_stage = (
                self._stage_records[index + 1]["stage_name"]
                if index + 1 < len(self._stage_records)
                else None
            )
            if details.get("final_evidence_sufficiency"):
                final_evidence_sufficiency_checks.append(
                    {
                        "stage_id": item["stage_id"],
                        "stage_status": item["status"],
                        **details["final_evidence_sufficiency"],
                        "actual_next_stage": next_stage,
                    }
                )
            if details.get("reviewer_information_adjudication"):
                reviewer_information_adjudication_checks.append(
                    {
                        "stage_id": item["stage_id"],
                        "stage_status": item["status"],
                        **details["reviewer_information_adjudication"],
                        "actual_next_stage": next_stage,
                    }
                )
        return {
            "run": {
                "paper_id": self.paper_id,
                "run_id": self.run_id,
                "status": status,
                "start_time": _iso(self.started_at),
                "end_time": _iso(finished),
                "duration_ms": _elapsed_ms(self._monotonic_started),
                "entrypoint": self.run_identity.get("entrypoint"),
                "input_identity": self.run_identity.get("input_identity", {}),
            },
            "environment": {
                "git_branch": _git_identity()[0],
                "git_commit": _git_identity()[1],
                "model_provider": self.model_provider,
                "model_name": self.model_name,
                "python_version": platform.python_version(),
            },
            "stages": stages,
            "tool_calls": list(tool_stats.values()),
            "provider_requests": _summarize_provider_requests(
                self._provider_request_records
            ),
            "llm_usage": _summarize_llm_usage(self._llm_call_records),
            "errors": list(self._error_records),
            "diagnostics": [
                {
                    "diagnostic_name": item.get("diagnostic_name"),
                    "schema_version": item.get("schema_version"),
                    "status": item.get("status"),
                    "artifact": (
                        f"diagnostics/{item.get('diagnostic_name')}.json"
                    ),
                    "counts": item.get("counts", {}),
                    "classification_counts": item.get(
                        "classification_counts", {}
                    ),
                    "primary_code": (item.get("verdict") or {}).get(
                        "primary_code"
                    ),
                }
                for item in self._diagnostic_results
            ],
            "outcome": self._outcome,
            "integrity_gates": integrity_gates,
            "final_evidence_sufficiency_checks": (
                final_evidence_sufficiency_checks
            ),
            "reviewer_information_adjudication_checks": (
                reviewer_information_adjudication_checks
            ),
            "last_completed_stage": completed[-1]["stage_name"] if completed else None,
        }

    def _coerce_error(
        self,
        error: BaseException | str | Mapping[str, Any] | None,
        *,
        stage: str | None,
        tool_call_id: str | None,
    ) -> dict[str, Any] | None:
        if error is None:
            return None
        if isinstance(error, BaseException):
            return self._error_payload(error, stage=stage, tool_call_id=tool_call_id)
        if isinstance(error, Mapping):
            payload = dict(error)
            payload.setdefault("type", "RuntimeError")
            payload.setdefault("message", "")
            payload.setdefault("traceback", "")
            payload.setdefault("stage", stage)
            payload.setdefault("tool_call_id", tool_call_id)
            payload.setdefault("timestamp", _iso(_now()))
            return payload
        error_type, separator, message = error.partition(":")
        if not separator or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.]*", error_type):
            error_type, message = "ToolExecutionError", error
        return {
            "type": error_type,
            "message": message.strip(),
            "traceback": "".join(traceback_module.format_stack()),
            "stage": stage,
            "tool_call_id": tool_call_id,
            "timestamp": _iso(_now()),
        }

    def _error_payload(
        self,
        exc: BaseException,
        *,
        stage: str | None,
        tool_call_id: str | None = None,
    ) -> dict[str, Any]:
        return {
            "type": type(exc).__name__,
            "message": str(exc),
            **({"code": str(exc.code)} if isinstance(getattr(exc, "code", None), str) else {}),
            **({"audit": exc.audit} if isinstance(getattr(exc, "audit", None), dict) else {}),
            "traceback": "".join(
                traceback_module.format_exception(type(exc), exc, exc.__traceback__)
            ),
            "stage": stage,
            "tool_call_id": tool_call_id,
            "timestamp": _iso(_now()),
        }

    def _persist_error(self, error: Mapping[str, Any]) -> None:
        self._error_counter += 1
        payload = dict(error)
        payload["error_id"] = f"error_{self._error_counter:04d}"
        self._error_records.append(payload)
        path = self.run_dir / "errors" / f"{self._error_counter:04d}.json"
        self._write_json(path, payload)

    def _write_json(self, path: Path, value: Any) -> None:
        serializable = _prepare_value(
            value, max_inline_bytes=self.config.max_inline_bytes,
            blob_writer=self._store_blob,
        )
        _atomic_write_text(
            path, json.dumps(serializable, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        )

    def _store_blob(self, payload: bytes) -> dict[str, Any]:
        digest = hashlib.sha256(payload).hexdigest()
        relative = Path("blobs") / digest
        path = self.run_dir / relative
        if not path.exists():
            path.write_bytes(payload)
        return {"type": "content_reference", "path": relative.as_posix(),
                "size": len(payload), "sha256": digest}

    @staticmethod
    def _find_record(records: list[dict[str, Any]], key: str, value: str) -> dict[str, Any]:
        for record in records:
            if record[key] == value:
                return record
        raise KeyError(value)


def current_runtime_artifacts() -> RuntimeArtifactManager | None:
    """Return the active run recorder, if runtime debugging is enabled."""

    manager = _current_run.get()
    return manager if manager is not None and manager.config.enabled else None


def _new_run_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    return f"run-{stamp}-{uuid.uuid4().hex[:10]}"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _elapsed_ms(started: float) -> int:
    return max(0, int((time.monotonic() - started) * 1000))


def _elapsed_seconds(started: float) -> float:
    return round(max(0.0, time.monotonic() - started), 6)


def _safe_segment(value: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", value.strip()).strip("._")
    if not safe or safe in {".", ".."}:
        raise ValueError("runtime artifact path identifier is empty or unsafe")
    return safe[:160]


def _public_record(record: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in record.items()
        if key not in {"path", "directory"} and not key.startswith("_")
    }


def _scope_from_stage_input(value: Any) -> dict[str, Any]:
    scope = dict(_current_scope.get())
    if not isinstance(value, Mapping):
        return scope
    point = value.get("current_point") or value.get("novelty_point")
    task = value.get("current_task") or value.get("research_task")
    if point is not None:
        scope["point_id"] = (point.get("point_id") if isinstance(point, Mapping)
                             else getattr(point, "point_id", None))
    if task is not None:
        for target, field in (("task_id", "task_id"),
                              ("attempt", "attempt"),
                              ("point_id", "novelty_point_id")):
            candidate = task.get(field) if isinstance(task, Mapping) else getattr(task, field, None)
            if candidate is not None:
                scope[target] = candidate
    return scope


def _prepare_value(value: Any, *, max_inline_bytes: int, key: str | None = None,
                   blob_writer: Any = None) -> Any:
    if key is not None and _is_sensitive_key(key):
        return _REDACTED
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    elif dataclasses.is_dataclass(value) and not isinstance(value, type):
        value = dataclasses.asdict(value)
    if isinstance(value, Mapping):
        return {
            str(item_key): _prepare_value(
                item_value, max_inline_bytes=max_inline_bytes, key=str(item_key),
                blob_writer=blob_writer,
            )
            for item_key, item_value in value.items()
        }
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_prepare_value(item, max_inline_bytes=max_inline_bytes,
                               blob_writer=blob_writer) for item in value]
    if isinstance(value, Path):
        return _path_reference(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Enum):
        return _prepare_value(value.value, max_inline_bytes=max_inline_bytes,
                              blob_writer=blob_writer)
    if isinstance(value, bytes):
        if blob_writer is not None:
            return blob_writer(value)
        return {
            "type": "content_reference",
            "size": len(value),
            "sha256": hashlib.sha256(value).hexdigest(),
            "summary": "binary content omitted from runtime artifact",
        }
    if isinstance(value, str):
        redacted = _SENSITIVE_TEXT.sub(lambda match: f"{match.group(1)}={_REDACTED}", value)
        redacted = _SENSITIVE_URL_VALUE.sub(lambda match: f"{match.group(1)}{_REDACTED}", redacted)
        redacted = _URL_USERINFO.sub(lambda match: f"{match.group(1)}{_REDACTED}@", redacted)
        encoded = redacted.encode("utf-8")
        if len(encoded) > max_inline_bytes:
            if blob_writer is not None:
                return blob_writer(encoded)
            return {
                "type": "content_reference",
                "size": len(encoded),
                "sha256": hashlib.sha256(encoded).hexdigest(),
                "summary": f"large text omitted ({len(encoded)} bytes)",
            }
        return redacted
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return _prepare_value(repr(value), max_inline_bytes=max_inline_bytes,
                          blob_writer=blob_writer)


def _is_sensitive_key(key: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]", "", key.lower())
    return normalized in _SENSITIVE_KEYS or normalized.endswith(
        ("apikey", "token", "secret", "password", "authorization", "cookie")
    )


def _path_reference(path: Path) -> dict[str, Any]:
    payload: dict[str, Any] = {"type": "artifact_reference", "path": str(path)}
    try:
        stat = path.stat()
        payload["size"] = stat.st_size
        if path.is_file():
            digest = hashlib.sha256()
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
            payload["sha256"] = digest.hexdigest()
    except OSError as exc:
        payload["summary"] = f"path metadata unavailable: {type(exc).__name__}"
    return payload


def _infer_result_count(value: Any) -> int | None:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="python")
    if isinstance(value, (list, tuple, set, frozenset)):
        return len(value)
    if isinstance(value, Mapping):
        # Reader observations may also contain an empty material_catalog. Count
        # the returned slices first so a successful content read is not logged
        # as EMPTY merely because the catalog is empty.
        if isinstance(value.get("read_results"), list):
            return sum(isinstance(item, Mapping) and
                       isinstance(item.get("char_start"), int) and
                       isinstance(item.get("char_end"), int) and
                       item["char_end"] > item["char_start"]
                       for item in value["read_results"])
        if isinstance(value.get("read_result"), Mapping):
            read = value["read_result"]
            return int(isinstance(read.get("char_start"), int) and
                       isinstance(read.get("char_end"), int) and
                       read["char_end"] > read["char_start"])
        for key in ("results", "items", "hits", "evidence_cards", "artifacts"):
            candidate = value.get(key)
            if isinstance(candidate, (list, tuple)):
                return len(candidate)
        for candidate in value.values():
            inferred = _infer_result_count(candidate)
            if inferred is not None:
                return inferred
    return None


def _summarize_provider_requests(
    records: list[dict[str, Any]], *, by_transport: bool = True
) -> dict[str, Any]:
    logical = [item for item in records if item.get("event_type") == "logical_request"]
    physical = [item for item in records if item.get("event_type") == "physical_request"]
    metadata_logical = [item for item in logical if item.get("operation") == "metadata"]
    metadata_physical = [item for item in physical if item.get("operation") == "metadata_batch"]
    batch_sizes = [int(item.get("unique_id_count") or 0) for item in metadata_physical]
    status_counts: dict[str, int] = {}
    for item in physical:
        key = str(item.get("status_code") or "transport_error")
        status_counts[key] = status_counts.get(key, 0) + 1
    recorded_times = [
        datetime.fromisoformat(str(item["recorded_at"]))
        for item in records
        if item.get("recorded_at")
    ]
    summary = {
        "transports": sorted({item.get("transport", "api") for item in records}),
        "logical_api_requests": sum(item.get("transport", "api") == "api" for item in logical),
        "physical_api_requests": sum(item.get("transport", "api") == "api" for item in physical),
        "logical_web_requests": sum(item.get("transport") == "web" for item in logical),
        "physical_web_requests": sum(item.get("transport") == "web" for item in physical),
        "timeout_count": sum("Timeout" in (item.get("error_type") or "") for item in physical),
        "circuit_open_count": sum(item.get("event_type") == "circuit_transition" and item.get("circuit_state") == "OPEN" for item in records),
        "interval_violation_count": sum(bool(item.get("interval_violation")) for item in physical),
        "metadata_logical_requests": len(metadata_logical),
        "metadata_physical_requests": len(metadata_physical),
        "unique_metadata_ids": sum(batch_sizes),
        "batch_count": len(metadata_physical),
        "average_batch_size": sum(batch_sizes) / len(batch_sizes) if batch_sizes else 0.0,
        "max_batch_size": max(batch_sizes, default=0),
        "dedup_count": sum(
            int(item.get("logical_request_count") or 0)
            - int(item.get("unique_id_count") or 0)
            for item in metadata_physical
        ),
        "status_counts": status_counts,
        "http_200_count": status_counts.get("200", 0),
        "http_429_count": status_counts.get("429", 0),
        "read_timeout_count": sum(
            item.get("error_type") == "ReadTimeout" for item in physical
        ),
        "retry_count": sum(int(item.get("attempt") or 1) > 1 for item in physical),
        "queue_wait_ms": sum(float(item.get("queue_wait_ms") or 0) for item in physical),
        "api_elapsed_ms": sum(float(item.get("elapsed_ms") or 0) for item in physical),
        "request_compression_ratio": len(logical) / len(physical) if physical else 0.0,
        "metadata_batch_ratio": len(metadata_logical) / len(metadata_physical) if metadata_physical else 0.0,
        "total_arxiv_elapsed_ms": (
            (max(recorded_times) - min(recorded_times)).total_seconds() * 1000
            if recorded_times
            else 0.0
        ),
    }
    if by_transport:
        summary["by_transport"] = {
            transport: _summarize_provider_requests(
                [item for item in records if item.get("transport", "api") == transport],
                by_transport=False,
            )
            for transport in summary["transports"]
        }
    return summary


def _format_known_cost(totals: Mapping[str, Any]) -> str:
    amount = totals.get("amount_rmb")
    if isinstance(amount, (int, float)):
        return f"{amount:.8f}"
    subtotal = totals.get("known_amount_rmb", 0)
    return f"Unknown (priced subtotal {subtotal:.8f})"


def _summarize_llm_usage(records: list[dict[str, Any]]) -> dict[str, Any]:
    def blank(label: str | None = None) -> dict[str, Any]:
        return {
            **({"model": label} if label is not None else {}),
            "calls": 0,
            "successful_calls": 0,
            "failed_calls": 0,
            "priced_calls": 0,
            "free_calls": 0,
            "unpriced_calls": 0,
            "usage_unavailable_calls": 0,
            "chat_transport_calls": 0,
            "context_measurement_requests": 0,
            "context_pre_dispatch_rejections": 0,
            "input_tokens": 0,
            "cached_input_tokens": 0,
            "output_tokens": 0,
            "reasoning_tokens": 0,
            "total_tokens": 0,
            "amount_rmb": 0.0,
        }

    totals = blank()
    by_model: dict[str, dict[str, Any]] = {}
    for record in records:
        model = str(record.get("model") or "unknown")
        targets = (totals, by_model.setdefault(model, blank(model)))
        tokens = record.get("tokens", {})
        billing = record.get("billing", {})
        billing_status = billing.get("status")
        for target in targets:
            target["calls"] += 1
            target["chat_transport_calls"] += int(bool(record.get("chat_transport_invoked")))
            target["context_measurement_requests"] += record.get("context_measurement_requests", 0)
            target["context_pre_dispatch_rejections"] += int(
                (record.get("context_admission") or {}).get("decision") == "reject"
                and not record.get("chat_transport_invoked"))
            status_key = (
                "successful_calls"
                if record.get("status") == "SUCCESS"
                else "failed_calls"
            )
            target[status_key] += 1
            if billing_status == "PRICED":
                target["priced_calls"] += 1
            elif billing_status == "FREE":
                target["free_calls"] += 1
            elif billing_status in {"UNPRICED", "PRICING_UNAVAILABLE"}:
                target["unpriced_calls"] += 1
            else:
                target["usage_unavailable_calls"] += 1
            for key in (
                "input_tokens",
                "cached_input_tokens",
                "output_tokens",
                "reasoning_tokens",
                "total_tokens",
            ):
                value = tokens.get(key, 0)
                if isinstance(value, int) and not isinstance(value, bool):
                    target[key] += value
            amount = billing.get("amount")
            if isinstance(amount, (int, float)) and not isinstance(amount, bool):
                target["amount_rmb"] = round(target["amount_rmb"] + amount, 8)

    for target in (totals, *by_model.values()):
        incomplete = bool(target["unpriced_calls"] or target["usage_unavailable_calls"])
        target["cost_completeness"] = "PARTIAL" if incomplete else "COMPLETE"
        # Keep the priced subtotal distinct from the unknown total. A failed
        # transport can still have incurred provider charges; absent usage is
        # not proof of a zero bill, nor is an unpriced local model free compute.
        target["known_amount_rmb"] = target["amount_rmb"]
        if incomplete:
            target["amount_rmb"] = None
    totals["currency"] = "RMB"
    return {"totals": totals, "by_model": list(by_model.values())}


def _stage_debug_details(
    stage_name: str,
    *,
    stage_input: Any,
    stage_output: Any,
    runtime_config: Mapping[str, Any],
) -> dict[str, Any] | None:
    if stage_name == "review_evidence":
        return _reviewer_debug_details(stage_input, stage_output)
    if stage_name != "check_final_evidence_sufficiency":
        return None
    state = _as_mapping(stage_input)
    if state is None:
        return None
    brief = _as_mapping(state.get("brief")) or {}
    point_values = brief.get("novelty_points", state.get("novelty_points", []))
    point_ids = [
        point_id
        for point in _as_sequence(point_values)
        if (point_id := _field_value(point, "point_id")) is not None
    ]
    counts = {point_id: 0 for point_id in point_ids}
    ignored_card_count = 0
    cards = _as_sequence(state.get("evidence_cards", []))
    for card in cards:
        point_id = _field_value(card, "novelty_point_id")
        if point_id in counts:
            counts[point_id] += 1
        else:
            ignored_card_count += 1

    workflow_config = _workflow_runtime_config(runtime_config)
    configured_cut = workflow_config.get("min_final_evidence_cards_per_point")
    if not isinstance(configured_cut, int) or isinstance(configured_cut, bool):
        configured_cut = _required_count_from_output(stage_output)
    if configured_cut is None:
        return None

    point_results = [
        {
            "novelty_point_id": point_id,
            "valid_card_count": counts[point_id],
            "required_card_count": configured_cut,
            "status": "PASS" if counts[point_id] >= configured_cut else "INSUFFICIENT",
        }
        for point_id in point_ids
    ]
    calculated_insufficient = [
        item["novelty_point_id"]
        for item in point_results
        if item["status"] == "INSUFFICIENT"
    ]
    calculated_insufficient_facts = [
        {
            "novelty_point_id": item["novelty_point_id"],
            "valid_card_count": item["valid_card_count"],
            "required_card_count": item["required_card_count"],
            "reason": "insufficient_final_evidence",
        }
        for item in point_results
        if item["status"] == "INSUFFICIENT"
    ]
    reported = _reported_insufficient_points(stage_output)
    rounds = state.get("rounds")
    max_rounds = workflow_config.get("max_rounds")
    output = _as_mapping(stage_output) or {}
    decisions = output.get("recovery_decisions")
    actionable = (any(_field_value(d, "action") not in {"stop", "none", "manual"}
                      for d in _as_sequence(decisions)) if decisions is not None else bool(reported))
    round_limit_allows_supplement = (
        actionable
        and isinstance(rounds, int)
        and isinstance(max_rounds, int)
        and rounds < max_rounds
    )
    return {
        "final_evidence_sufficiency": {
            "configured_cut": configured_cut,
            "round": rounds,
            "max_rounds": max_rounds,
            "input_final_valid_card_count": len(cards),
            "counted_final_valid_card_count": sum(counts.values()),
            "ignored_card_count": ignored_card_count,
            "calculated_status": (
                "INSUFFICIENT" if calculated_insufficient else "PASS"
            ),
            "check_status": (
                ("INSUFFICIENT" if reported else "PASS")
                if reported is not None
                else None
            ),
            "point_results": point_results,
            "calculated_insufficient_point_ids": calculated_insufficient,
            "calculated_insufficient_final_evidence_points": (
                calculated_insufficient_facts
            ),
            "reported_insufficient_final_evidence_points": reported,
            "output_matches_calculation": (
                reported == calculated_insufficient_facts
                if reported is not None
                else None
            ),
            "round_limit_allows_supplement": round_limit_allows_supplement,
            "recovery_decisions": [dict(_as_mapping(d) or {}) for d in _as_sequence(decisions)] if decisions is not None else None,
            "routing_basis": "recovery_decisions" if decisions is not None else "legacy_card_count",
        }
    }


def _reviewer_debug_details(
    stage_input: Any, stage_output: Any
) -> dict[str, Any] | None:
    """Build an IDs-only Reviewer audit without copying source text."""

    state = _as_mapping(stage_input)
    if state is None:
        return None
    output = _as_mapping(stage_output) or {}
    points = _as_sequence(state.get("novelty_points", []))
    point_ids = [
        point_id
        for point in points
        if (point_id := _field_value(point, "point_id")) is not None
    ]
    cards = _as_sequence(
        state.get("validator_accepted_cards") or state.get("evidence_cards", [])
    )
    evidence = _as_sequence(state.get("raw_evidence", []))
    evidence_by_id = {
        evidence_id: item
        for item in evidence
        if (evidence_id := _field_value(item, "evidence_id")) is not None
    }

    point_scopes = []
    all_unresolved: set[str] = set()
    for point_id in point_ids:
        point_cards = [
            item for item in cards
            if _field_value(item, "novelty_point_id") == point_id
        ]
        card_ids = [
            value for item in point_cards
            if (value := _field_value(item, "card_id")) is not None
        ]
        referenced_ids = sorted(
            {
                str(evidence_id)
                for card in point_cards
                for evidence_id in (
                    (_as_mapping(card) or {}).get("evidence_ids", []) or []
                )
                if isinstance(evidence_id, str) and evidence_id
            }
        )
        unresolved = sorted(
            evidence_id
            for evidence_id in referenced_ids
            if evidence_id not in evidence_by_id
            or _field_value(evidence_by_id[evidence_id], "novelty_point_id")
            != point_id
        )
        all_unresolved.update(unresolved)
        allowed_artifact_ids = sorted(
            {
                artifact_id
                for evidence_id in referenced_ids
                if evidence_id in evidence_by_id
                and _field_value(evidence_by_id[evidence_id], "novelty_point_id")
                == point_id
                if (
                    artifact_id := _field_value(
                        evidence_by_id[evidence_id], "artifact_id"
                    )
                )
                is not None
            }
        )
        point_scopes.append(
            {
                "novelty_point_id": point_id,
                "card_ids": card_ids,
                "referenced_evidence_ids": referenced_ids,
                "unresolved_evidence_ids": unresolved,
                "allowed_artifact_ids": allowed_artifact_ids,
            }
        )

    reviews = _as_sequence(output.get("novelty_reviews", []))
    review_ids = [
        value for item in reviews
        if (value := _field_value(item, "novelty_point_id")) is not None
    ]
    duplicate_review_ids = sorted(
        {value for value in review_ids if review_ids.count(value) > 1}
    )
    review_results = []
    for item in reviews:
        mapping = _as_mapping(item) or {}
        supplement = _as_mapping(mapping.get("supplement_request"))
        review_results.append(
            {
                "novelty_point_id": mapping.get("novelty_point_id"),
                "status": mapping.get("status"),
                "verdict": mapping.get("verdict"),
                "confidence": mapping.get("confidence"),
                "highly_relevant_work_count": len(
                    _as_sequence(mapping.get("highly_relevant_works", []))
                ),
                "has_supplement_request": supplement is not None,
                "supplement_reason": (
                    supplement.get("reason") if supplement is not None else None
                ),
            }
        )

    input_card_ids = [
        value for item in cards
        if (value := _field_value(item, "card_id")) is not None
    ]
    output_cards = _as_sequence(output.get("evidence_cards", []))
    output_card_ids = [
        value for item in output_cards
        if (value := _field_value(item, "card_id")) is not None
    ]
    return {
        "reviewer_information_adjudication": {
            "expected_point_ids": point_ids,
            "reviewed_point_ids": review_ids,
            "missing_review_point_ids": sorted(set(point_ids) - set(review_ids)),
            "unexpected_review_point_ids": sorted(set(review_ids) - set(point_ids)),
            "duplicate_review_point_ids": duplicate_review_ids,
            "input_card_ids": input_card_ids,
            "output_card_ids": output_card_ids,
            "cards_preserved": (
                len(input_card_ids) == len(output_card_ids)
                and set(input_card_ids) == set(output_card_ids)
            ),
            "unresolved_evidence_ids": sorted(all_unresolved),
            "point_scopes": point_scopes,
            "review_results": review_results,
            "supplement_requests_control_route": False,
        }
    }


def _as_mapping(value: Any) -> Mapping[str, Any] | None:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="python")
    elif dataclasses.is_dataclass(value) and not isinstance(value, type):
        value = dataclasses.asdict(value)
    return value if isinstance(value, Mapping) else None


def _as_sequence(value: Any) -> list[Any] | tuple[Any, ...]:
    return value if isinstance(value, (list, tuple)) else []


def _field_value(value: Any, field: str) -> str | None:
    mapping = _as_mapping(value)
    candidate = (
        mapping.get(field) if mapping is not None else getattr(value, field, None)
    )
    return candidate if isinstance(candidate, str) and candidate else None


def _workflow_runtime_config(runtime_config: Mapping[str, Any]) -> Mapping[str, Any]:
    workflow = runtime_config.get("workflow")
    if isinstance(workflow, Mapping):
        return workflow
    project = runtime_config.get("project")
    if isinstance(project, Mapping):
        workflow = project.get("workflow")
        if isinstance(workflow, Mapping):
            return workflow
    return {}


def _reported_insufficient_points(stage_output: Any) -> list[dict[str, Any]] | None:
    output = _as_mapping(stage_output)
    if output is None or "insufficient_final_evidence_points" not in output:
        return None
    reported = []
    for item in _as_sequence(output["insufficient_final_evidence_points"]):
        mapping = _as_mapping(item)
        if mapping is not None:
            reported.append(dict(mapping))
    return reported


def _required_count_from_output(stage_output: Any) -> int | None:
    reported = _reported_insufficient_points(stage_output)
    if not reported:
        return None
    value = reported[0].get("required_card_count")
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _stage_validation_result(stage_name: str, output: Any) -> dict[str, Any] | None:
    if not isinstance(output, Mapping):
        return None
    key = {
        "validate_evidence": "evidence_integrity",
        "validate_synthesis_input": "synthesis_integrity",
        "validate_report_integrity": "report_integrity",
    }.get(stage_name)
    value = output.get(key) if key is not None else None
    return dict(value) if isinstance(value, Mapping) else None


def _git_identity() -> tuple[str | None, str | None]:
    def run(*args: str) -> str | None:
        try:
            completed = subprocess.run(
                ["git", *args], capture_output=True, text=True, timeout=2, check=False
            )
        except (OSError, subprocess.SubprocessError):
            return None
        value = completed.stdout.strip()
        return value if completed.returncode == 0 and value else None

    return run("branch", "--show-current"), run("rev-parse", "HEAD")


def _atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", newline="\n", dir=path.parent, prefix=f".{path.name}.",
            suffix=".tmp", delete=False
        ) as handle:
            temporary = Path(handle.name)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _render_summary(summary: Mapping[str, Any]) -> str:
    run = summary["run"]
    environment = summary["environment"]
    lines = [
        "# Runtime Summary", "", "## Run", "",
        f"Paper ID: {run['paper_id']}", f"Run ID: {run['run_id']}",
        f"Entrypoint: {run.get('entrypoint') or '-'}",
        f"Input Identity: {json.dumps(run.get('input_identity', {}), ensure_ascii=False)}",
        f"Status: {run['status']}", f"Start: {run['start_time']}",
        f"End: {run['end_time']}", f"Duration: {run['duration_ms']} ms", "",
        "## Environment", "", f"Git Branch: {environment['git_branch']}",
        f"Git Commit: {environment['git_commit']}",
        f"Model: {environment['model_provider']} / {environment['model_name']}",
        f"Python: {environment['python_version']}", "", "## Stage Status", "",
        "| Stage | Status | Duration |", "|---|---|---:|",
    ]
    lines.extend(
        f"| {item['stage_name']} | {item['status']} | "
        f"{item.get('duration_ms') if item.get('duration_ms') is not None else '-'} ms |"
        for item in summary["stages"]
    )
    lines.extend(["", "## Tool Calls", "", "| Tool | Calls | Success | Failed | Empty |",
                  "|---|---:|---:|---:|---:|"])
    lines.extend(
        f"| {item['tool_name']} | {item['calls']} | {item['success']} | "
        f"{item['failed']} | {item['empty']} |"
        for item in summary["tool_calls"]
    )
    lines.extend(
        [
            "",
            "## Runtime Diagnostics",
            "",
            "| Diagnostic | Status | Calls | Failed | Primary finding | Detail |",
            "|---|---|---:|---:|---|---|",
        ]
    )
    diagnostics = summary.get("diagnostics", [])
    if diagnostics:
        lines.extend(
            f"| {item.get('diagnostic_name')} | {item.get('status')} | "
            f"{item.get('counts', {}).get('reader_calls', 0)} | "
            f"{item.get('counts', {}).get('failed', 0)} | "
            f"{item.get('primary_code') or '-'} | `{item.get('artifact')}` |"
            for item in diagnostics
        )
        for item in diagnostics:
            counts = item.get("classification_counts", {})
            if counts:
                lines.extend(
                    [
                        "",
                        f"{item.get('diagnostic_name')} classifications: "
                        + ", ".join(
                            f"{code}={count}" for code, count in sorted(counts.items())
                        ),
                    ]
                )
    else:
        lines.append("| - | SKIPPED | 0 | 0 | - | - |")
    outcome = summary.get("outcome")
    lines.extend(["", "## Run Outcome", ""])
    if isinstance(outcome, Mapping):
        lines.extend(f"- {key}: {value}" for key, value in outcome.items())
    else:
        lines.append("- None")
    llm_usage = summary.get("llm_usage", {})
    llm_totals = llm_usage.get("totals", {})
    lines.extend(
        [
            "",
            "## LLM Token Usage and Cost",
            "",
            f"Calls: {llm_totals.get('calls', 0)}",
            f"Input tokens: {llm_totals.get('input_tokens', 0)}",
            f"Cached input tokens: {llm_totals.get('cached_input_tokens', 0)}",
            f"Output tokens: {llm_totals.get('output_tokens', 0)}",
            f"Reasoning tokens: {llm_totals.get('reasoning_tokens', 0)}",
            f"Total tokens: {llm_totals.get('total_tokens', 0)}",
            f"Estimated API cost (RMB): {_format_known_cost(llm_totals)}",
            f"Cost completeness: {llm_totals.get('cost_completeness', 'COMPLETE')}",
            "",
            "| Model | Calls | Input | Cached | Output | Total | Cost (RMB) | Unpriced |",
            "|---|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    lines.extend(
        f"| {item['model']} | {item['calls']} | {item['input_tokens']} | "
        f"{item['cached_input_tokens']} | {item['output_tokens']} | "
        f"{item['total_tokens']} | {_format_known_cost(item)} | "
        f"{item['unpriced_calls']} |"
        for item in llm_usage.get("by_model", [])
    )
    lines.extend(["", "## Final Evidence Sufficiency Checks", ""])
    checks = summary.get("final_evidence_sufficiency_checks", [])
    if checks:
        for check in checks:
            lines.extend(
                [
                    f"### Round {check.get('round')}",
                    "",
                    f"Configured cut: {check.get('configured_cut')}",
                    f"Stage status: {check.get('stage_status')}",
                    f"Check status: {check.get('check_status')}",
                    f"Input final valid Cards: {check.get('input_final_valid_card_count')}",
                    f"Ignored Cards: {check.get('ignored_card_count')}",
                    f"Output matches calculation: {check.get('output_matches_calculation')}",
                    "Round limit allows supplement: "
                    f"{check.get('round_limit_allows_supplement')}",
                    f"Actual next stage: {check.get('actual_next_stage')}",
                    "",
                    "| Novelty Point | Valid Cards | Required Cards | Status |",
                    "|---|---:|---:|---|",
                ]
            )
            lines.extend(
                f"| {item['novelty_point_id']} | {item['valid_card_count']} | "
                f"{item['required_card_count']} | {item['status']} |"
                for item in check.get("point_results", [])
            )
            lines.append("")
    else:
        lines.append("- None")
    lines.extend(["", "## Reviewer Information Adjudication", ""])
    reviewer_checks = summary.get("reviewer_information_adjudication_checks", [])
    if reviewer_checks:
        for check in reviewer_checks:
            lines.extend(
                [
                    f"### Stage {check.get('stage_id')}",
                    "",
                    f"Stage status: {check.get('stage_status')}",
                    f"Cards preserved: {check.get('cards_preserved')}",
                    "Supplement requests control route: "
                    f"{check.get('supplement_requests_control_route')}",
                    f"Missing reviews: {check.get('missing_review_point_ids')}",
                    f"Unexpected reviews: {check.get('unexpected_review_point_ids')}",
                    f"Duplicate reviews: {check.get('duplicate_review_point_ids')}",
                    f"Unresolved Evidence IDs: {check.get('unresolved_evidence_ids')}",
                    f"Actual next stage: {check.get('actual_next_stage')}",
                    "",
                    "| Novelty Point | Status | Verdict | Relevant Works | Supplement |",
                    "|---|---|---|---:|---|",
                ]
            )
            lines.extend(
                f"| {item.get('novelty_point_id')} | {item.get('status')} | "
                f"{item.get('verdict') or '-'} | "
                f"{item.get('highly_relevant_work_count')} | "
                f"{item.get('has_supplement_request')} |"
                for item in check.get("review_results", [])
            )
            lines.append("")
    else:
        lines.append("- None")
    lines.extend(["", "## Errors", ""])
    if summary["errors"]:
        lines.extend(
            f"- {item.get('stage') or '-'} / {item.get('tool_call_id') or '-'}: "
            f"{item.get('type')}: {item.get('message')}"
            for item in summary["errors"]
        )
    else:
        lines.append("- None")
    lines.extend(["", "## Integrity Gates", ""])
    if summary.get("integrity_gates"):
        lines.extend(
            f"- {item['stage_name']}: "
            f"{'PASS' if item.get('validation_passed') else 'FAILED'}"
            for item in summary["integrity_gates"]
        )
        for item in summary["integrity_gates"]:
            for issue in item.get("issues", []):
                lines.append(f"  - {issue}")
            for rejected in item.get("rejected_cards", []):
                for reason in rejected.get("reasons", []):
                    lines.append(f"  - {rejected.get('card_id')}: {reason}")
    else:
        lines.append("- None")
    lines.extend(["", "## Last Completed Stage", "",
                  str(summary["last_completed_stage"] or "None"), ""])
    return "\n".join(lines)


__all__ = [
    "RuntimeArtifactManager",
    "RuntimeDebugConfig",
    "StageHandle",
    "ToolCallHandle",
    "current_runtime_artifacts",
]
