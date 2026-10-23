"""Task-owned accounting and audit utilities; action selection stays in each node."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
import hashlib
import json
from copy import deepcopy

from .runtime_artifacts import current_runtime_artifacts

from . import correction_trace as audit
from ..schemas.failures import (FailureCode, FailureScope, RecoveryAction,
                                CodedFailure, ArtifactVersionRef, make_failure)

current_session = ContextVar("local_correction_session", default=None)
retry_queries = ContextVar("local_correction_retry_queries", default=None)


@dataclass
class LocalSession:
    model_limit: int
    tool_limit: int
    read_limit: int
    correction_model_limit: int = 3
    correction_model_used: int = 0
    per_tool_limits: dict = field(default_factory=dict)
    tool_counts: dict = field(default_factory=dict)
    model_used: int = 0
    tool_used: int = 0
    read_used: int = 0
    attempts: dict = field(default_factory=dict)
    records: dict = field(default_factory=dict)
    checkpoint_path: Path | None = None

    def attach(self, request):
        runtime = current_runtime_artifacts()
        if runtime is None or runtime.run_dir is None:
            return
        identity = json.dumps([request.run_id, request.subject_paper_id,
            request.novelty_point.point_id, request.research_task.task_id, request.research_task.attempt])
        self.checkpoint_path = runtime.run_dir / "local_correction_budgets" / (hashlib.sha256(identity.encode()).hexdigest() + ".json")
        if self.checkpoint_path.exists():
            from ..schemas.failures import CorrectionRecord
            saved = json.loads(self.checkpoint_path.read_text())
            budget = saved["budget"]
            for kind in ("model", "tool", "read", "correction_model"):
                if budget[kind + "_limit"] != getattr(self, kind + "_limit"):
                    raise ValueError("Cannot change original task budget during correction recovery")
                setattr(self, kind + "_used", max(getattr(self, kind + "_used"), budget[kind + "_used"]))
            if self.per_tool_limits != budget["per_tool_limits"]:
                raise ValueError("Cannot change per-tool limits during correction recovery")
            for name, count in budget["tool_counts"].items():
                self.tool_counts[name] = max(self.tool_counts.get(name, 0), count)
            self.attempts.update(budget["attempts"])
            self.records.update({row["correction_id"]: CorrectionRecord.model_validate(row) for row in saved["records"]})
        self.persist()

    def persist(self):
        if self.checkpoint_path is not None:
            from ..persistence import _atomic_write_json
            self.checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
            _atomic_write_json(self.checkpoint_path, {"budget": self.snapshot(),
                "records": [r.model_dump(mode="json") for r in self.records.values()]})

    def check(self, kind, amount=1):
        if amount < 0:
            raise ValueError("Budget charges must be nonnegative")
        used, limit = getattr(self, kind + "_used"), getattr(self, kind + "_limit")
        if used + amount > limit:
            raise CodedFailure({"model": FailureCode.BUDGET_TURNS,
                                "tool": FailureCode.BUDGET_TOOLS,
                                "read": FailureCode.BUDGET_READ_CHARS}[kind],
                               "Original task budget exhausted during local correction")

    def charge(self, kind, amount=1):
        self.check(kind, amount)
        setattr(self, kind + "_used", getattr(self, kind + "_used") + amount)
        self.persist()

    def check_tool(self, name):
        self.check("tool")
        limit = self.per_tool_limits.get(name)
        if limit is not None and self.tool_counts.get(name, 0) >= limit:
            raise CodedFailure(FailureCode.BUDGET_PER_TOOL, "Original task per-tool limit exhausted")

    def charge_tool(self, name):
        self.check_tool(name)
        self.tool_used += 1
        self.tool_counts[name] = self.tool_counts.get(name, 0) + 1
        self.persist()

    def charge_correction_model(self):
        if self.correction_model_used >= self.correction_model_limit:
            raise CodedFailure(FailureCode.BUDGET_TURNS, "Local correction model allowance exhausted")
        self.check("model")
        self.model_used += 1
        self.correction_model_used += 1
        self.persist()

    def once(self, key):
        if self.attempts.get(key, 0):
            return False
        self.attempts[key] = 1
        self.persist()
        return True

    def close_pending(self):
        for record in list(self.records.values()):
            if record.status.value not in {"verification_passed", "stopped"}:
                stop(record, "Task ended before pending correction completed; no automatic replay")

    def snapshot(self):
        return deepcopy({name: getattr(self, name) for name in (
            "model_limit", "tool_limit", "read_limit", "model_used", "tool_used",
            "read_used", "attempts", "correction_model_limit", "correction_model_used",
            "per_tool_limits", "tool_counts")})

    @contextmanager
    def bind(self):
        token = current_session.set(self)
        try:
            yield self
        finally:
            try:
                self.close_pending()
                self.persist()
            finally:
                current_session.reset(token)


def charge(kind, amount=1):
    session = current_session.get()
    if session is not None:
        session.charge(kind, amount)


def charge_tool(name):
    session = current_session.get()
    if session is not None:
        session.charge_tool(name)


def charge_correction_model():
    session = current_session.get()
    if session is not None:
        session.charge_correction_model()


def save(record):
    session = current_session.get()
    if session is not None:
        session.records[record.correction_id] = record
        session.persist()
    return record


def detect(scope, code, owner, *, message, inputs=None, targets=None, failure=None, versions=None):
    failure = failure or make_failure(code, scope=FailureScope(
        paper_id=scope.subject_paper_id, run_id=getattr(scope, "run_id", None),
        point_id=scope.novelty_point.point_id,
        task_id=getattr(getattr(scope, "research_task", None), "task_id", None),
        node=owner), message=message)
    return save(audit.record_detection(failure, owner_node=owner, owner_function=owner,
        action_input={**(inputs or {}), "task_budget_before": current_session.get().snapshot() if current_session.get() else None},
        targets=targets, artifact_versions_before=versions or [], budget={"local_limit": 1, "local_used": 0}))


def begin(record, action):
    record = audit.record_advice(record, action)
    return save(audit.record_execution(record, attempt=1, budget={"local_limit": 1, "local_used": 1}))


def finish(record, passed, checker, *, result=None, reason=None, versions=None):
    record = audit.record_verification_pending(record, result={**(result or {}),
        "task_budget_after": current_session.get().snapshot() if current_session.get() else None})
    record = audit.record_verification(record, passed=passed, checker=checker,
        artifact_versions_after=versions,
        remaining_issues=[reason or "Local correction verification failed"] if not passed else [])
    if not passed:
        record = audit.record_stop(record, reason=reason or "Local correction attempt exhausted")
    return save(record)


def stop(record, reason):
    return save(audit.record_stop(record, reason=reason, remaining_issues=[reason]))


def read_versions(reads):
    return list({(r.namespace, r.artifact_id, r.sha256): ArtifactVersionRef(
        namespace=r.namespace.value, artifact_id=r.artifact_id, sha256=r.sha256)
        for r in reads}.values())


async def audited_call(name, arguments, invoke):
    """Log an explicitly selected post-Harness/internal call without selecting actions."""
    import inspect
    runtime = current_runtime_artifacts()
    handle = (runtime.start_tool_call(name, agent_arguments={}, resolved_arguments=arguments,
                                     agent_tool_call_id=None) if runtime is not None else None)
    try:
        result = invoke()
        if inspect.isawaitable(result):
            result = await result
        if runtime is not None and handle is not None:
            payload = result.model_dump(mode="json") if hasattr(result, "model_dump") else result
            runtime.finish_tool_call(handle, raw_result=payload, normalized_result=payload,
                succeeded=getattr(result, "succeeded", True), error=getattr(result, "error", None),
                failure_phase=None if getattr(result, "succeeded", True) else "TOOL_EXECUTION")
        return result
    except BaseException as exc:
        if runtime is not None and handle is not None:
            runtime.fail_tool_call(handle, exc)
        raise
