"""Generic tool protocol and registry for task-scoped Researchers."""

from __future__ import annotations

import re
import time
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
        started = time.monotonic()
        try:
            validated = self.validate_arguments(tool_name, arguments)
            return await self.execute_validated(
                tool_name, validated, scope=scope, started=started
            )
        except Exception as exc:
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

        invoked_at = time.monotonic() if started is None else started
        correction_key = _correction_key(scope, tool_name)
        pending = self._pending_corrections.pop(correction_key, None)
        if pending is not None:
            from ..core.correction_trace import record_execution
            pending = record_execution(
                pending,
                attempt=pending.attempt + 1,
                budget={
                    "local_limit": pending.attempt + 1,
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
                    record_stop(
                        completed,
                        reason='The corrected request executed but the registered tool still reported failure.',
                        remaining_issues=completed.remaining_issues,
                    )
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
                record_stop(
                    failed,
                    reason='The corrected request raised at the registered execution boundary.',
                    remaining_issues=failed.remaining_issues,
                )
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
    correction = record_detection(
        failure,
        owner_node=f'{tool_name}.{phase}',
        owner_function='ResearcherToolRegistry.execute',
        action_input={
            'tool_name': tool_name,
            'arguments': _json_arguments(arguments),
        },
        targets=_correction_targets(arguments),
    )
    if failure.recovery in {RecoveryAction.MANUAL, RecoveryAction.STOP,
                            RecoveryAction.NONE}:
        correction = record_stop(
            correction,
            reason='The registered boundary has no automatic local correction for this failure.',
            action=failure.recovery,
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
        tool_name,
    )


def _remember_pending_correction(pending, key, payload):
    from ..schemas.failures import CorrectionRecord, CorrectionStatus
    correction = payload.get('correction')
    if correction is None:
        return
    record = CorrectionRecord.model_validate(correction)
    if record.status == CorrectionStatus.ADVISED:
        pending[key] = record


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
