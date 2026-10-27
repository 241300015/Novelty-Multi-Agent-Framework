"""Harness-owned concept acceptance barrier and isolated strategy generation.

No model selects stages. Each stage has its own bounded repair loop; strategy
requests and repairs can only edit strategy drafts. Accepted concepts are stored
as immutable JSON and reconstructed for compilation, never taken from stage 2.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass
from typing import Any, TYPE_CHECKING, Literal

from backend.env import ModelClientError
from backend.env.model_client import ModelCallBudgetExceeded
from .failure_classification import model_failure_code
from .planner_repair import PlannerRepairHarness
from .runtime_artifacts import current_runtime_artifacts
from ..schemas import NoveltyPoint, ResearchTask, SearchPlan, SearchPlanDraft
from ..schemas.search_plan_draft import SearchConceptsDraft, SearchStrategiesDraft
from ..schemas.failures import CodedFailure, FailureCode as C
from ..agents.search_plan_compiler import (SearchPlanCompilationError, build_runtime_plan,
    validate_concept_semantics, validate_strategy_semantics)

if TYPE_CHECKING:
    from ..agents.search_planner import SearchPlannerAgent

PlanningStage = Literal["concepts", "strategies"]


@dataclass(frozen=True)
class AcceptedConceptCatalog:
    serialized: str

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.serialized.encode()).hexdigest()

    def concepts(self):
        return SearchConceptsDraft.model_validate_json(self.serialized).concepts

    def view(self):
        return [{"concept_id": f"C{i+1}", **concept.model_dump(mode="json")}
                for i, concept in enumerate(self.concepts())]


class SearchPlannerHarness:
    def __init__(self, agent: SearchPlannerAgent):
        self.agent = agent

    async def run(self, point: NoveltyPoint, task: ResearchTask, *, use_async_client: bool) -> SearchPlan:
        concepts = await self._run_stage(point, task, use_async_client=use_async_client,
            stage="concepts", catalog=None)
        catalog = AcceptedConceptCatalog(concepts.model_dump_json())
        runtime = current_runtime_artifacts()
        if runtime is not None:
            runtime.record_planner_event({"point_id": point.point_id, "task_id": task.task_id,
                "planning_stage": "concepts", "step": "concept_catalog_frozen",
                "payload": {"catalog_sha256": catalog.digest, "concepts": catalog.view()}})
        return await self._run_stage(point, task, use_async_client=use_async_client,
            stage="strategies", catalog=catalog)

    def _validate_stage(self, draft, *, stage, catalog, task):
        if stage == "concepts":
            issues = validate_concept_semantics(draft, task=task, limits=self.agent.semantic_limits)
            if issues:
                raise SearchPlanCompilationError("概念阶段检查失败", issues=issues)
            return draft
        issues = validate_strategy_semantics(draft, task=task,
            concept_ids={row["concept_id"] for row in catalog.view()})
        if issues:
            raise SearchPlanCompilationError("策略阶段检查失败", issues=issues)
        return build_runtime_plan(SearchPlanDraft(concepts=catalog.concepts(), strategies=draft.strategies),
                                  task=task, limits=self.agent.semantic_limits)

    async def _run_stage(self, point, task, *, use_async_client: bool,
                         stage: PlanningStage, catalog: AcceptedConceptCatalog | None):
        # Keep the historical public error classes on the model adapter.
        from ..agents.search_planner import (SearchPlannerExhaustedError, _DraftOutputError,
            _TRANSIENT_MODEL_CODES, _transport_retry_delay, _validate_draft_data)
        draft_model = SearchConceptsDraft if stage == "concepts" else SearchStrategiesDraft
        schema = draft_model.model_json_schema()
        if catalog is not None:
            schema["$defs"]["SearchStrategyDraft"]["properties"]["focus_concepts"]["items"] = {
                "type": "string", "enum": [row["concept_id"] for row in catalog.view()]}
        retry_reason = ""
        history: list[dict[str, Any]] = []
        transport_retries = repairs = 0
        repair = None
        runtime = current_runtime_artifacts()

        def record(attempt: int, step: str, payload: Any) -> None:
            if runtime is not None:
                runtime.record_planner_event({
                    "point_id": point.point_id, "task_id": task.task_id,
                    "attempt": attempt, "planning_stage": stage, "catalog_sha256": catalog.digest if catalog else None,
                    "step": step, "payload": payload,
                })

        for attempt_index in range(self.agent.max_attempts):
            attempt = attempt_index + 1
            data = None
            phase = "model_call"
            try:
                if use_async_client:
                    data = await self.agent._acomplete_json(
                        point=point, task=task, retry_reason=retry_reason,
                        stage=stage, schema=schema, catalog=catalog,
                    )
                else:
                    data = self.agent._complete_json(
                        point=point, task=task, retry_reason=retry_reason,
                        stage=stage, schema=schema, catalog=catalog,
                    )
                record(attempt, "parsed_model_json", data)
                if repair is not None:
                    phase = "repair_admission"
                    data = repair.accept(data)
                    record(attempt, "repair_scope_checked", repair.audit())
                    repair.begin_revalidation()
                phase = "draft_validation"
                draft = _validate_draft_data(data, model=draft_model, allow_wrapper=False)
                record(attempt, "validated_draft", draft)
                phase = "compilation"
                plan = self._validate_stage(draft, stage=stage, catalog=catalog, task=task)
                phase = "recording"
                record(attempt, "concepts_accepted" if stage == "concepts" else "compiled_plan", plan)
                if repair is not None:
                    repair.finish(succeeded=True)
                    record(attempt, "repair_revalidated", repair.audit())
                if history:
                    record(attempt, "recovery_succeeded", {"history": history})
                return plan
            except Exception as exc:
                # Cancellation is BaseException and must propagate without repair.
                output = data
                delay = 0.0
                issues: list[dict[str, Any]] = []
                action = "stop"
                if isinstance(exc, SearchPlanCompilationError):
                    category, step = "plan_compilation_error", "compilation_failed"
                    if exc.issues:
                        issues = [{"code": issue.failure_code.value, "detail": issue.detail,
                                   "fix": issue.fix, "paths": [list(p) for p in issue.paths]}
                                  for issue in exc.issues]
                        code, action = C(issues[0]["code"]), "repair_output"
                    else:
                        code = C.PLANNER_COMPILE_INVALID
                elif isinstance(exc, _DraftOutputError):
                    category, step = "invalid_model_output", "draft_validation_failed"
                    code, issues, output = exc.code, exc.issues, exc.output
                    if code != C.MODEL_OUTPUT_TRUNCATED:
                        action = "repair_output"
                elif isinstance(exc, (ModelClientError, ModelCallBudgetExceeded, TimeoutError)):
                    category, step = "model_client_error", "model_call_failed"
                    code = model_failure_code(exc)
                    if code in _TRANSIENT_MODEL_CODES:
                        delay = _transport_retry_delay(exc)
                        if delay <= 30:
                            action = "retry_request"
                elif isinstance(exc, CodedFailure):
                    category, step = "repair_protocol_error", "repair_rejected"
                    code = exc.code
                    issues = [{"code": code.value, "detail": str(exc)}]
                    if code in {C.PLANNER_REPAIR_SCOPE, C.PLANNER_REPAIR_NO_PROGRESS} and repair is not None and phase == "repair_admission":
                        action = "restrict_repair"
                        issues[0]["paths"] = [list(p) for p in repair.rejected_paths]
                else:
                    # A client JSON decoder error is a response-envelope fault,
                    # not malformed model-authored draft text.
                    code = (C.PLANNER_COMPILE_INVALID if phase == "compilation" else
                            C.MODEL_RESPONSE_SHAPE if isinstance(exc, json.JSONDecodeError) else C.UNKNOWN)
                    category, step = "internal_error", "planning_failed"

                pending_repair = None
                if action == "repair_output" and repairs == 0:
                    pending_repair = PlannerRepairHarness.prepare(
                        output=output, issues=issues,
                        task_key=[point.point_id, task.task_id, task.attempt, stage, catalog.digest if catalog else None],
                        draft_schema=schema,
                    )
                    if pending_repair is None:
                        action = "stop"
                terminal_reason = "non_retryable"
                if action != "stop":
                    if attempt >= self.agent.max_attempts:
                        terminal_reason = "attempt_limit"
                    elif action == "restrict_repair" and repair.protocol_corrections >= 1:
                        terminal_reason = "protocol_budget_exhausted"
                    elif action == "repair_output" and repairs >= 1:
                        terminal_reason = "repair_budget_exhausted"
                    elif action == "retry_request" and transport_retries >= 1:
                        terminal_reason = "transport_budget_exhausted"
                    else:
                        terminal_reason = ""
                if repair is not None and terminal_reason:
                    repair.finish(succeeded=False)
                    record(attempt, "repair_terminated", repair.audit())
                event = {"attempt": attempt, "planning_stage": stage, "node": phase, "code": code.value, "issues": issues,
                         "action": action if not terminal_reason else "stop",
                         "terminal_reason": terminal_reason,
                         "retry_delay_seconds": delay if not terminal_reason else 0}
                if repair is not None:
                    event["repair"] = repair.audit()
                history.append(event)
                record(attempt, step, {**event, "error": str(exc), "previous_output": output})
                if terminal_reason:
                    error = SearchPlannerExhaustedError(
                        novelty_point_id=point.point_id, task_id=task.task_id, attempts=attempt,
                        last_error=str(exc), failure_category=category, code=code,
                        terminal_reason=terminal_reason, history=history, planning_stage=stage,
                        catalog_sha256=catalog.digest if catalog else None,
                    )
                    record(attempt, "planning_terminated", error.audit)
                    raise error from exc
                if action == "repair_output":
                    repairs += 1
                    repair = pending_repair
                    assert repair is not None
                    record(attempt, "repair_prepared", repair.feedback())
                    retry_reason = json.dumps(repair.feedback(), ensure_ascii=False)
                elif action == "restrict_repair":
                    repair.restrict()
                    record(attempt, "repair_protocol_restricted", repair.feedback())
                    retry_reason = json.dumps(repair.feedback(), ensure_ascii=False)
                else:
                    transport_retries += 1
                    # Preserve the last repair context verbatim across transport failures.
                    await asyncio.sleep(delay)
        raise AssertionError("Planner attempt loop exited without a terminal outcome")
