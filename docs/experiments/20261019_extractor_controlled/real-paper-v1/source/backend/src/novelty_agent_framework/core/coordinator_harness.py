"""Trigger-selected executable policies, choice-only Reader loop and decision binding."""
from __future__ import annotations

import asyncio
import json
import re
from dataclasses import replace

from backend.env import ModelCallOptions

from ..schemas.failures import FailureCode as C, FailureEvent, FailureScope, RecoveryDecision, make_failure
from ..schemas.supplement_planning import CoordinatorRecoveryDraft, SupplementPlanningInput, SupplementPlanningResult
from .failure_classification import model_failure_code, output_failure_code
from .coordinator_policy import COORDINATOR_WORK_SPECS, CoordinatorPlanningPolicy, work_spec
from .recovery_planning import validate_supplement_tasks, task_validation_error
from .tool_call_harness import ToolCallHarness, ToolCallHarnessConfig


class CoordinatorPlanningError(RuntimeError):
    def __init__(self, code, audit, failures):
        super().__init__("Coordinator Harness 未生成合法补检决策")
        self.code, self.harness_audit, self.failure_events = code, audit, failures


class _AsyncClient:
    """Use the same model client/budget; support synchronous client adapters."""
    def __init__(self, client):
        self.client = client

    async def acomplete(self, messages, *, options=None):
        method = getattr(self.client, "acomplete", None)
        if method is not None:
            return await method(messages, options=options)
        return await asyncio.to_thread(self.client.complete, messages, options=options)

    def __getattr__(self, name):
        return getattr(self.client, name)


def trigger_inputs(request):
    """One independently scoped input per trigger, including all affected points."""
    for code in COORDINATOR_WORK_SPECS:
        selected = [d for d in request.recovery_decisions if d.trigger_codes == [code]]
        if not selected:
            continue
        points = {d.point_id for d in selected}
        yield code, SupplementPlanningInput.model_validate({**request.model_dump(),
            "recovery_decisions": selected,
            "brief": {**request.brief.model_dump(),
                      "novelty_points": [p for p in request.brief.novelty_points if p.point_id in points]},
            "existing_evidence": [c for c in request.existing_evidence if c.novelty_point_id in points],
            "insufficient_final_evidence_points": [g for g in request.insufficient_final_evidence_points
                                                   if g.novelty_point_id in points],
            "research_history": [h for h in request.research_history if h.point_id in points],
            "material_index": [r for r in request.material_index
                               if (r.point_id is None or r.point_id in points)
                               and (r.kind != "recovery_decision" or r.trigger_code == code)],
        })


def _audit_trace(trace, request):
    records, failures = [], {}
    for i, event in enumerate(trace):
        record = {"kind": event.kind, "detail": event.detail}
        if event.tool_call:
            record["tool_call"] = {"id": event.tool_call.id, "name": event.tool_call.name,
                                   "arguments": event.tool_call.arguments}
        if event.observation:
            record["observation"] = event.observation.model_dump(mode="json")
            payload = event.observation.payload
            values = [*payload.get("execution_issues", [])]
            if payload.get("failure"):
                values.append(payload["failure"])
            for value in values:
                failure = FailureEvent.model_validate(value)
                failures[failure.event_id] = failure
        if event.failure_code:
            record["failure_code"] = event.failure_code.value
            failure = make_failure(event.failure_code,
                scope=FailureScope(node="coordinator.harness", paper_id=request.paper.paper_id,
                                   run_id=request.run_id, attempt=request.attempt),
                message=event.detail or "Coordinator harness execution issue",
                occurrence_id=f"attempt-{request.attempt}-{request.recovery_decisions[0].trigger_codes[0]}-event-{i}")
            failures[failure.event_id] = failure
        records.append(record)
    return records, list(failures.values())


def bind_coordinator_draft(draft, *, request, brief, reads, guidance, origin="coordinator_llm"):
    """Validate LLM choices and preserve immutable runtime trigger facts."""
    by_point = {d.point_id: d for d in request.recovery_decisions}
    codes = {c for d in request.recovery_decisions for c in d.trigger_codes}
    if codes != {draft.trigger_code}:
        raise ValueError("LLM output must belong to this single trigger-code workflow")
    if len({d.point_id for d in draft.decisions}) != len(draft.decisions) or {
        d.point_id for d in draft.decisions
    } != by_point.keys():
        raise ValueError("Coordinator 决策包含未知查新点或未覆盖全部获准查新点")
    model_tasks = []
    for d in draft.decisions:
        if d.action != COORDINATOR_WORK_SPECS[draft.trigger_code].action:
            raise ValueError("Coordinator 动作不符合本点获准触发码")
        if any(t.novelty_point_id != d.point_id for t in d.research_tasks):
            raise task_validation_error(C.COORDINATOR_TASK_BINDING, "补检任务引用未知查新点或与所属决策不一致")
        if len(set(d.basis_read_ids)) != len(d.basis_read_ids):
            raise ValueError("duplicate planning read references")
        for read_id in d.basis_read_ids:
            read = reads.get(read_id)
            if read is None or read["point_id"] not in {None, d.point_id} or read["char_end"] <= read["char_start"]:
                raise ValueError("Coordinator 引用了未读取、空内容或其他查新点的 Reader 记录")
        model_tasks.extend(d.research_tasks)
    tasks = validate_supplement_tasks(model_tasks, brief=brief, decisions=request.recovery_decisions,
                                     languages=request.research_languages, attempt=request.attempt,
                                     trigger_code=draft.trigger_code)
    decisions = [RecoveryDecision.model_validate({**by_point[d.point_id].model_dump(),
        "action": d.action, "reason": d.reason, "coordinator_status": "planned", "planning_origin": origin,
        "planning_basis_read_ids": d.basis_read_ids,
        "coordinator_guidance_ids": [g["guidance_id"] for g in guidance if d.point_id in g["point_ids"]],
        "planned_task_ids": [t.task_id for t in tasks if t.novelty_point_id == d.point_id],
    }) for d in draft.decisions]
    return SupplementPlanningResult(brief=brief.model_copy(update={"research_tasks": tasks}),
                                    recovery_decisions=decisions)


class CoordinatorRecoveryHarness:
    def __init__(self, client, *, prompts, output_root, config=None):
        # tools.__init__ also imports legacy Agents; defer until this module is initialized.
        from ..tools.coordinator_reader import CoordinatorReaderTool
        from ..tools.researcher_registry import ResearcherToolRegistry
        self.prompts = prompts
        self.registry = ResearcherToolRegistry([CoordinatorReaderTool(output_root)])
        self.client = _AsyncClient(client)
        self.config = replace(config or ToolCallHarnessConfig(
            max_turns=6, max_tool_calls=4, per_tool_limits={"reader": 4}, max_total_read_chars=24000,
            finalize_on_budget=True, reserve_final_turn=True, report_budget=True,
            allow_one_read_budget_correction=True,
            finalization_instruction="Reader 预算已结束。只用已收到的摘要和读取记录，输出 CoordinatorRecoveryDraft JSON。"
                                     "不得再调用工具，不得编造读取记录、解除 STOP 或改写触发事实。",
        ), text_choices=True)

    async def run(self, *, request, brief, prompt_name, options=None):
        tasks, decisions, failures, audits = [], [], [], []
        for code, scoped in trigger_inputs(request):
            try:
                result = await self._run_trigger(request=scoped, brief=brief, prompt_name=prompt_name, options=options)
            except CoordinatorPlanningError as exc:
                event = make_failure(exc.code, scope=FailureScope(node="coordinator.plan_supplement",
                    paper_id=request.paper.paper_id, run_id=request.run_id, attempt=request.attempt),
                    message=f"Coordinator planning failed for trigger {code}; other triggers remain independent.",
                    occurrence_id=f"attempt-{request.attempt}-{code}")
                stopped = [RecoveryDecision.model_validate({**d.model_dump(), "action": "stop",
                    "reason": f"{code} 补检规划失败；保留未解决缺口。", "coordinator_status": "failed",
                    "max_additional_attempts": 0,
                    "cause_event_ids": list(dict.fromkeys([*d.cause_event_ids, event.event_id]))})
                    for d in scoped.recovery_decisions]
                result = SupplementPlanningResult(brief=brief.model_copy(update={"research_tasks": []}),
                    recovery_decisions=stopped, failure_events=[*exc.failure_events, event],
                    harness_audit=exc.harness_audit)
            tasks.extend(result.brief.research_tasks)
            decisions.extend(result.recovery_decisions)
            failures.extend(result.failure_events)
            audits.append({"trigger_code": code, "input": scoped.model_dump(mode="json"),
                           **result.harness_audit})
        return SupplementPlanningResult(brief=brief.model_copy(update={"research_tasks": tasks}),
            recovery_decisions=decisions, failure_events=failures, harness_audit={"trigger_workflows": audits})

    async def _run_trigger(self, *, request, brief, prompt_name, options=None):
        audit, trace = {"guidance": [], "trace": []}, ()
        policy = None
        try:
            code, spec = work_spec(request)
            guidance = [spec.record(code, [d.point_id for d in request.recovery_decisions])]
            audit["guidance"] = guidance
            policy = CoordinatorPlanningPolicy(self.registry, request,
                max_reads=min(self.config.max_tool_calls, self.config.per_tool_limits.get("reader", self.config.max_tool_calls)),
                max_read_chars=self.config.max_total_read_chars)
            loop = ToolCallHarness(self.client, self.registry, config=self.config,
                action_policy_factory=lambda registry, scope: policy)
            payload = request.model_dump(mode="json")
            rendered = self.prompts.render(prompt_name,
                **{f"{key}_json": json.dumps(value, ensure_ascii=False) for key, value in payload.items()
                   if key != "attempt"},
                attempt=request.attempt, planning_input_json=json.dumps(payload, ensure_ascii=False),
                trigger_code=request.recovery_decisions[0].trigger_codes[0],
                decision_schema=json.dumps(CoordinatorRecoveryDraft.model_json_schema(), ensure_ascii=False))
            result = await loop.run(scope=request,
                system_prompt=rendered.system,
                initial_user_message=rendered.user,
                options=replace(options or ModelCallOptions(), response_format={"type": "json_object"},
                                tool_choice="none"))
            trace = result.trace
            audit.update(tool_calls_used=result.tool_calls_used, turns_used=result.turns_used,
                         stop_reason=result.stop_reason, final_content=result.final_content)
            audit["trace"], failures = _audit_trace(trace, request)
            audit["policy"] = policy.audit(forced_reason=result.stop_reason)
            content = (result.final_content or "").strip()
            fence = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", content, re.DOTALL)
            draft = CoordinatorRecoveryDraft.model_validate_json(fence.group(1) if fence else content)
            reads = {e.observation.payload["read_result"]["read_id"]: e.observation.payload["read_result"]
                     for e in trace if e.observation and e.observation.succeeded
                     and e.observation.tool_name == "reader" and "read_result" in e.observation.payload}
            planned = bind_coordinator_draft(draft, request=request, brief=brief, reads=reads, guidance=guidance)
            return planned.model_copy(update={"failure_events": failures, "harness_audit": audit})
        except Exception as exc:
            if policy is not None:
                audit.setdefault("policy", policy.audit())
            trace = getattr(exc, "trace", trace)
            audit["trace"], failures = _audit_trace(trace, request)
            # Preserve the typed underlying model/budget cause from the serial Harness.
            code = next((e.failure_code for e in reversed(trace) if e.failure_code and e.kind == "error"), None)
            if not getattr(exc, "trace", None):
                code = output_failure_code(exc) if isinstance(exc, (ValueError, TypeError)) else model_failure_code(exc)
            code = code or C.UNKNOWN
            audit["failure_code"] = code.value
            raise CoordinatorPlanningError(code, audit, failures) from exc
