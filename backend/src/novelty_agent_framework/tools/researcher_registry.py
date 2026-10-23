"""Generic tool protocol and registry for task-scoped Researchers."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
import uuid
from collections.abc import Mapping
from typing import Any, Protocol

from ..schemas import (
    ResearcherToolObservation,
    StrictModel,
)
from ..core.target_paper_identity import (
    artifact_is_target,
    filter_target_observation,
    source_record_is_target,
)


class ResearcherTool(Protocol):
    name: str
    description: str
    args_schema: type[StrictModel]

    async def ainvoke(
        self, arguments: StrictModel, *, scope: Any
    ) -> ResearcherToolObservation: ...


class ResearcherToolRegistry:
    def __init__(self, tools: list[ResearcherTool] | None = None) -> None:
        self._tools: dict[str, ResearcherTool] = {}
        self._blocked_target_artifacts: dict[tuple[str, str], set[str]] = {}
        self._blocked_target_records: dict[tuple[str, str], set[str]] = {}
        self._pending_corrections: dict[tuple[str | None, ...], Any] = {}
        self._stopped_corrections: dict[tuple, Any] = {}
        self._correction_locks: dict[tuple, asyncio.Lock] = {}
        for tool in tools or []:
            self.register(tool)

    def register(self, tool: ResearcherTool) -> None:
        name = tool.name.strip()
        if not name:
            raise ValueError("tool name cannot be empty")
        if name in self._tools:
            raise ValueError(f"duplicate researcher tool {name!r}")
        self._tools[name] = tool

    def get(self, name: str) -> ResearcherTool:
        try:
            return self._tools[name]
        except KeyError as exc:
            raise ValueError(f"unregistered researcher tool {name!r}") from exc

    def descriptions(self) -> list[dict[str, Any]]:
        return [
            {
                "name": tool.name,
                "description": tool.description,
                "arguments_schema": tool.args_schema.model_json_schema(),
            }
            for tool in self._tools.values()
        ]

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(self._tools)

    async def execute(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        scope: Any,
    ) -> ResearcherToolObservation:
        # Keep virtual dispatch: RecoveryToolRegistry owns additional scope gates.
        try:
            validated = self.validate_arguments(tool_name, arguments)
        except Exception:
            key = _correction_key(scope, tool_name)
            async with self._correction_locks.setdefault(key, asyncio.Lock()):
                return await self._execute(tool_name, arguments, scope=scope)
        return await self.execute_validated(tool_name, validated, scope=scope)

    async def _execute(self, tool_name, arguments, *, scope):
        started = time.monotonic()
        key = _correction_key(scope, tool_name)
        if key in self._stopped_corrections:
            return self._stopped_observation(tool_name, arguments, key)
        try:
            validated = self.validate_arguments(tool_name, arguments)
        except Exception as exc:
            pending = self._pending_corrections.pop(key, None)
            if pending is not None:
                from ..core.correction_trace import record_execution, record_verification, record_stop
                from ..schemas.failures import FAILURE_CATALOG
                pending = record_execution(pending, attempt=pending.attempt + 1,
                    budget={"local_limit": FAILURE_CATALOG[pending.failure_code].retry_limit,
                            "local_used": pending.attempt + 1})
                failed = record_verification(pending, passed=False,
                    checker="ResearcherToolRegistry.validate_arguments",
                    details={"error_type": type(exc).__name__},
                    remaining_issues=[_safe_error(exc)])
                stopped = record_stop(failed, reason="Argument correction retry limit reached.")
                self._stopped_corrections[key] = stopped
                return self._stopped_observation(tool_name, arguments, key)
            payload = _failure_payload(
                exc, scope, tool_name,
                registered=tool_name in self._tools,
                phase="arguments",
                arguments=arguments,
            )
            _remember_pending_correction(
                self._pending_corrections,
                _correction_key(scope, tool_name),
                payload,
            )
            return ResearcherToolObservation(
                tool_name=tool_name,
                arguments=_json_arguments(arguments),
                succeeded=False,
                error=_safe_error(exc),
                payload=payload,
                elapsed_ms=int((time.monotonic() - started) * 1000),
            )
        return await self._execute_validated(tool_name, validated, scope=scope, started=started)

    def _stopped_observation(self, tool_name, arguments, key):
        record = self._stopped_corrections[key]
        return ResearcherToolObservation(tool_name=tool_name,
            arguments=arguments.model_dump(mode="json") if hasattr(arguments, "model_dump") else _json_arguments(arguments),
            succeeded=False, error=record.stop_reason,
            payload={"correction": record.model_dump(mode="json"), "execution_blocked": True})

    def validate_arguments(
        self, tool_name: str, arguments: Mapping[str, Any]
    ) -> StrictModel:
        """Return the canonical arguments, including schema-provided defaults."""

        tool = self.get(tool_name)
        return tool.args_schema.model_validate(dict(arguments))

    async def execute_validated(
        self,
        tool_name: str,
        arguments: StrictModel,
        *,
        scope: Any,
        started: float | None = None,
    ) -> ResearcherToolObservation:
        """Execute one already-canonicalized argument object."""

        key = _correction_key(scope, tool_name)
        async with self._correction_locks.setdefault(key, asyncio.Lock()):
            if key in self._stopped_corrections:
                return self._stopped_observation(tool_name, arguments, key)
            return await self._execute_validated(tool_name, arguments, scope=scope, started=started)

    async def _execute_validated(self, tool_name, arguments, *, scope, started=None):

        invoked_at = time.monotonic() if started is None else started
        correction_key = _correction_key(scope, tool_name)
        pending = self._pending_corrections.pop(correction_key, None)
        if pending is not None:
            from ..core.correction_trace import record_execution
            from ..schemas.failures import FAILURE_CATALOG
            pending = record_execution(
                pending,
                attempt=pending.attempt + 1,
                budget={
                    "local_limit": FAILURE_CATALOG[pending.failure_code].retry_limit,
                    "local_used": pending.attempt + 1,
                },
            )
        try:
            tool = self.get(tool_name)
            if not isinstance(arguments, tool.args_schema):
                raise TypeError(
                    f"validated arguments for {tool_name!r} use the wrong schema"
                )
            identity = getattr(scope, "target_identity", None)
            paper_id = getattr(scope, "subject_paper_id", None)
            run_id = getattr(scope, "run_id", None)
            gate_key = (run_id, paper_id) if identity and run_id and paper_id else None
            if gate_key and tool_name == "reader":
                reads = getattr(arguments, "reads", None)
                requests = reads if reads is not None else [arguments]
                for request in requests:
                    artifact_id = request.artifact_id
                    if (artifact_id in self._blocked_target_artifacts.get(gate_key, set()) or
                            artifact_is_target(tool, paper_id, artifact_id, identity)):
                        raise PermissionError("target paper artifact is excluded from Reader")
            if gate_key and tool_name == "database_search":
                for record_id in getattr(arguments, "full_text_source_record_ids", []):
                    if (record_id in self._blocked_target_records.get(gate_key, set()) or
                            source_record_is_target(tool.reference_store, paper_id, record_id, identity)):
                        raise PermissionError("target paper source record is excluded from full-text acquisition")
            from ..core.local_correction import charge_tool
            charge_tool(tool_name)
            observation = await tool.ainvoke(arguments, scope=scope)
            if gate_key and tool_name in {"database_search", "reference_search"}:
                observation, blocked = filter_target_observation(observation, identity)
                self._blocked_target_artifacts.setdefault(gate_key, set()).update(blocked)
                self._blocked_target_records.setdefault(gate_key, set()).update(
                    row["source_record_id"] for row in observation.payload.get("target_exclusions", [])
                    if row.get("source_record_id")
                )
            if pending is not None:
                from ..core.correction_trace import (
                    record_verification,
                    record_verification_pending,
                )
                pending = record_verification_pending(
                    pending,
                    result={
                        "tool_name": tool_name,
                        "succeeded": observation.succeeded,
                    },
                )
                completed = record_verification(
                    pending,
                    passed=observation.succeeded,
                    checker=f"ResearcherToolRegistry.execute_validated:{tool_name}",
                    details={"observation_succeeded": observation.succeeded},
                    remaining_issues=([] if observation.succeeded else [
                        observation.error or "corrected tool request still failed"
                    ]),
                )
                if not observation.succeeded:
                    from ..core.correction_trace import record_stop
                    stopped = record_stop(
                        completed,
                        reason='The corrected request executed but the registered tool still reported failure.',
                        remaining_issues=completed.remaining_issues,
                    )
                    self._stopped_corrections[correction_key] = stopped
            elif not observation.succeeded:
                observation = _audit_failed_observation(observation, scope, tool_name, arguments)
            return observation
        except Exception as exc:
            if pending is not None:
                from ..core.correction_trace import record_verification
                failed = record_verification(
                    pending,
                    passed=False,
                    checker=f"ResearcherToolRegistry.execute_validated:{tool_name}",
                    details={"error_type": type(exc).__name__},
                    remaining_issues=[_safe_error(exc)],
                    result={"tool_name": tool_name, "succeeded": False},
                )
                from ..core.correction_trace import record_stop
                stopped = record_stop(
                    failed,
                    reason='The corrected request raised at the registered execution boundary.',
                    remaining_issues=failed.remaining_issues,
                )
                self._stopped_corrections[correction_key] = stopped
                return self._stopped_observation(tool_name, arguments, correction_key)
            payload = _failure_payload(
                exc, scope, tool_name,
                registered=tool_name in self._tools,
                phase="execution",
                arguments=arguments.model_dump(mode="json"),
            )
            _remember_pending_correction(
                self._pending_corrections,
                correction_key,
                payload,
            )
            return ResearcherToolObservation(
                tool_name=tool_name,
                arguments=arguments.model_dump(mode="json"),
                succeeded=False,
                error=_safe_error(exc),
                payload=payload,
                elapsed_ms=int((time.monotonic() - invoked_at) * 1000),
            )

    def project_model_context(
        self,
        tool_name: str,
        observation: ResearcherToolObservation,
    ) -> dict[str, Any]:
        """Project a full audit observation into the model-visible data plane."""

        if not observation.succeeded:
            tool = self._tools.get(tool_name)
            projector = getattr(tool, "project_model_context", None)
            if projector is not None and any(key not in {"error_type", "failure"} for key in observation.payload):
                projected = projector(observation)
                if not isinstance(projected, dict):
                    raise TypeError("tool model-context projector must return a dict")
                return projected
            return {
                "succeeded": False,
                "summary": observation.summary,
                "error": observation.error,
                **observation.payload,
            }
        tool = self.get(tool_name)
        projector = getattr(tool, "project_model_context", None)
        if projector is None:
            return observation.model_dump(mode="json")
        projected = projector(observation)
        if not isinstance(projected, dict):
            raise TypeError("tool model-context projector must return a dict")
        return projected


def _safe_error(exc: Exception) -> str:
    message = re.sub(
        r"(?i)(api[_-]?key|authorization|cookie)\s*[:=]\s*\S+",
        r"\1=<redacted>",
        str(exc),
    )
    return f"{type(exc).__name__}: {message}"[:1000]


def _json_arguments(arguments: Any) -> dict[str, Any]:
    if not isinstance(arguments, dict):
        return {"invalid_arguments_type": type(arguments).__name__}
    return {
        str(key): value
        for key, value in arguments.items()
        if value is None or isinstance(value, (str, int, float, bool, list, dict))
    }


def _failure_payload(exc, scope, tool_name, *, registered, phase, arguments=None):
    from ..schemas.failures import (
        FailureCode as C,
        CodedFailure,
        FailureScope,
        RecoveryAction,
        make_failure,
    )
    if not registered:
        code = C.TOOL_NOT_REGISTERED
    elif isinstance(exc, CodedFailure):
        code = exc.code
    elif isinstance(exc, PermissionError) and (tool_name != 'reader' or exc.filename is None):
        code = C.TOOL_SCOPE_DENIED
    elif tool_name == 'reader' and isinstance(exc, OSError):
        from ..core.failure_classification import material_failure_code
        code = material_failure_code(exc)
    elif phase == 'arguments':
        code = C.TOOL_ARGUMENT_SCHEMA
    else:
        code = C.UNKNOWN
    task = getattr(scope, 'research_task', None)
    failure = make_failure(code, scope=FailureScope(
        node=f'{tool_name}.{phase}', paper_id=getattr(scope,'subject_paper_id',None), run_id=getattr(scope,'run_id',None),
        point_id=getattr(getattr(scope,'novelty_point',None),'point_id',None),
        task_id=getattr(task,'task_id',None), attempt=getattr(task,'attempt',None)),
        message='Tool request failed at the registered action boundary; no scientific finding was established.',
        occurrence_id=f'{tool_name}-{phase}-{type(exc).__name__}')
    from ..core.correction_trace import record_advice, record_detection, record_stop
    request_identity = hashlib.sha256(json.dumps(
        _json_arguments(arguments), sort_keys=True, ensure_ascii=False, default=str,
    ).encode("utf-8")).hexdigest()
    correction = record_detection(
        failure,
        owner_node=f'{tool_name}.{phase}',
        owner_function='ResearcherToolRegistry.execute',
        action_input={
            'tool_name': tool_name,
            'arguments': _json_arguments(arguments),
            'original_request_sha256': request_identity,
        },
        targets=_correction_targets(arguments),
    )
    if failure.recovery in {RecoveryAction.MANUAL, RecoveryAction.STOP,
                            RecoveryAction.NONE} or phase != "arguments":
        from ..schemas.failures import FAILURE_CATALOG
        terminal = (failure.recovery if failure.recovery in {
            RecoveryAction.STOP, RecoveryAction.MANUAL, RecoveryAction.NONE,
        } else next(action for action in (
            RecoveryAction.STOP, RecoveryAction.MANUAL, RecoveryAction.NONE,
        ) if action in FAILURE_CATALOG[code].actions))
        correction = record_stop(
            correction,
            reason='The registered boundary has no automatic local correction for this failure.',
            action=terminal,
            remaining_issues=[failure.code.value],
            result={'boundary_phase': phase, 'executed': False},
        )
    else:
        correction = record_advice(
            correction,
            failure.recovery,
            action_input={
                'tool_name': tool_name,
                'arguments': _json_arguments(arguments),
                'original_request_sha256': request_identity,
            },
            remaining_issues=[failure.code.value],
        )
    return {
        'error_type': type(exc).__name__,
        'failure': failure.model_dump(mode='json'),
        'correction': correction.model_dump(mode='json'),
    }


def _correction_key(scope, tool_name):
    point = getattr(scope, 'novelty_point', None)
    task = getattr(scope, 'research_task', None)
    return (
        getattr(scope, 'run_id', None),
        getattr(scope, 'subject_paper_id', None),
        getattr(point, 'point_id', None),
        getattr(task, 'task_id', None),
        getattr(task, 'attempt', None),
        tool_name,
    )


def _remember_pending_correction(pending, key, payload):
    from ..schemas.failures import CorrectionRecord, CorrectionStatus
    correction = payload.get('correction')
    if correction is None:
        return
    record = CorrectionRecord.model_validate(correction)
    if record.status == CorrectionStatus.ADVISED:
        if key in pending:
            raise ValueError("pending correction must be completed before replacement")
        pending[key] = record


def _audit_failed_observation(observation, scope, tool_name, arguments):
    """Record an observed failure without claiming an unexecuted recovery."""
    from ..schemas.failures import FailureCode, FailureScope, FailureEvent, make_failure, failure_family
    from ..core.correction_trace import record_detection, record_stop
    from ..core.local_correction import current_session
    # A node-owned terminal record must not be overwritten by a generic dispatch
    # failure. Transient query failures are resolved by the task-end owner.
    if observation.payload.get("correction"):
        return observation
    if tool_name == "database_search" and current_session.get() is not None:
        rows = observation.payload.get("search_executions", [])
        if isinstance(rows, list):
            for row in rows:
                try:
                    failure = FailureEvent.model_validate(row.get("failure"))
                except (ValueError, TypeError, AttributeError):
                    continue
                if failure_family(failure.code) in {FailureCode.PROVIDER_NETWORK,
                        FailureCode.PROVIDER_RATE_LIMIT, FailureCode.PROVIDER_SERVICE}:
                    return observation
    task = getattr(scope, "research_task", None)
    failure_scope = FailureScope(node=f"{tool_name}.execution",
        paper_id=getattr(scope, "subject_paper_id", None), run_id=getattr(scope, "run_id", None),
        point_id=getattr(getattr(scope, "novelty_point", None), "point_id", None),
        task_id=getattr(task, "task_id", None), attempt=getattr(task, "attempt", None))
    code = FailureCode.UNKNOWN
    supplied = observation.payload.get("failure")
    if isinstance(supplied, dict):
        try:
            code = FailureEvent.model_validate(supplied).code
        except ValueError:
            pass
    elif observation.payload.get("failure_code"):
        try:
            code = FailureCode(observation.payload["failure_code"])
        except ValueError:
            pass
    failure = make_failure(code, scope=failure_scope,
        message="Registered tool returned a failed observation; no recovery was executed.",
        occurrence_id=uuid.uuid4().hex)
    correction = record_detection(failure, owner_node=f"{tool_name}.execution",
        owner_function="ResearcherToolRegistry.execute_validated",
        action_input={"tool_name": tool_name, "arguments": _json_arguments(arguments.model_dump(mode="json"))},
        targets=_correction_targets(arguments))
    # STOP is not listed for every category; use its allowed terminal suggestion.
    from ..schemas.failures import FAILURE_CATALOG, RecoveryAction
    actions = FAILURE_CATALOG[code].actions
    terminal = next((a for a in (RecoveryAction.STOP, RecoveryAction.MANUAL, RecoveryAction.NONE) if a in actions), None)
    if terminal is None:
        from ..core.correction_trace import record_advice
        correction = record_advice(correction, actions[0], remaining_issues=[code.value])
    else:
        correction = record_stop(correction, action=terminal,
            reason="Tool returned failure; this boundary has no automatic recovery for the observation.",
            remaining_issues=[code.value], result={"succeeded": False, "recovery_executed": False})
    return observation.model_copy(update={"payload": {**observation.payload,
        "failure": failure.model_dump(mode="json"), "correction": correction.model_dump(mode="json")}})


def _correction_targets(arguments):
    if hasattr(arguments, 'model_dump'):
        arguments = arguments.model_dump(mode='json')
    if not isinstance(arguments, dict):
        return {}
    reads = arguments.get('reads')
    read_rows = reads if isinstance(reads, list) else [arguments]
    return {
        'source_ids': [arguments['source_id']] if isinstance(arguments.get('source_id'), str) else [],
        'source_record_ids': [value for value in (
            arguments.get('source_record_ids')
            or arguments.get('full_text_source_record_ids')
            or []
        ) if isinstance(value, str)],
        'material_ids': [row['artifact_id'] for row in read_rows
                         if isinstance(row, dict) and isinstance(row.get('artifact_id'), str)],
        'card_ids': [arguments['card_id']] if isinstance(arguments.get('card_id'), str) else [],
        'evidence_ids': [arguments['evidence_id']] if isinstance(arguments.get('evidence_id'), str) else [],
    }
