"""Controlled single-card orchestration, separate from point-level adjudication."""
from __future__ import annotations

import asyncio
import json
import time
from copy import deepcopy
from dataclasses import replace

from backend.env import ModelCallOptions
from pydantic import ValidationError

from ..core.format_repair import repair_json
from ..core.review_validation import feature_catalog, validate_review_references
from ..core.reviewer_evidence import reviewer_model_view
from ..core.tool_call_harness import ToolCallHarness, ToolCallHarnessConfig, ToolCallHarnessEvent
from ..response_language import with_chinese_explanations
from ..schemas import ReviewStatus
from ..schemas.domain import ReviewerSupportDraft, ReviewerSupportReview
from ..tools.reader import ReviewerReaderTool
from ..tools.researcher_registry import ResearcherToolRegistry
from ..workflows.reviewer_reading_policy import ReviewerReadingPolicy


SUPPORT_INSTRUCTION = """本轮只评估一张卡片的材料可用性和证据支撑，不作点级新颖性裁定。
先按菜单初评，然后决定是否读取；程序执行读取后必须重评，最后输出 ReviewerSupportDraft。
仅选择当前菜单 option_id。程序管理读取位置、预算和证据身份，不要调用其他函数。
仅引用 evidence_choices 中已提供的 evidence_id，不生成 read_id、引用位置或原文。
已保存/下载全文不等于已阅读。摘要或片段未提及某特征属于 unknown，不能证明未采用。
每项非 unknown 的特征关系须引用同 Work 的直接依据；是否支持、相关性和作者/基线归属仍需你判断。
区分导航、背景与技术依据。只说明已展示内容的支撑范围，保留未知和局限。
单卡可用性不代表点级结论，不输出 verdict、confidence 或程序执行字段。"""


def _strict_support_schema(schema: dict) -> dict:
    """Require explicit fields on the wire without changing stored model defaults.

    Walk schema keywords, not arbitrary annotations/property names. References
    stay intact; their definitions and every union/array branch are visited.
    Existing nullable types and non-unknown evidence constraints are preserved.
    """
    result = deepcopy(schema)

    def visit(node):
        if not isinstance(node, dict):
            return
        node.pop("default", None)  # A generation contract does not fill defaults.
        if node.get("type") == "object" or "properties" in node:
            node["required"] = list(node.get("properties", {}))
            node["additionalProperties"] = False
        for keyword in ("properties", "$defs", "definitions", "patternProperties"):
            for child in node.get(keyword, {}).values():
                visit(child)
        for keyword in ("items", "additionalProperties"):
            visit(node.get(keyword))
        for keyword in ("anyOf", "oneOf", "allOf", "prefixItems"):
            for child in node.get(keyword, []):
                visit(child)

    visit(result)
    return result


async def review_controlled_card(reviewer, request):
    # Import shared legacy helpers lazily: the public reviewer owns compatibility.
    from .evidence_reviewer import (_BudgetedClient, _extract_json, _record_review_event,
        _review_execution_facts, _review_exception_issue, _review_failure_cause)

    # Registry correction bookkeeping is mutable; tools themselves expose request-scoped I/O.
    registry = ResearcherToolRegistry([reviewer.tools.get(name) for name in reviewer.tools.names])
    policy = None
    result = None
    phase = "material"
    client = _BudgetedClient(reviewer._client(), reviewer.config.card_timeout_seconds,
                             max_calls=reviewer.config.max_steps)
    schema = _strict_support_schema(ReviewerSupportDraft.model_json_schema())
    options = replace(reviewer.model_options or ModelCallOptions(temperature=reviewer.config.temperature),
        response_format={"type": "json_schema", "json_schema": {
            "name": "reviewer_support_draft", "strict": True,
            "schema": schema}},
        tools=(), tool_choice="none")

    def state():
        return {**(policy.snapshot() if policy else {"stage": phase}),
                "model_calls_used": client.calls_used, "model_call_limit": reviewer.config.max_steps}

    def execution_issues():
        # The ledger alone owns successful read audits. Retain tool failures even
        # when a deadline cancels the harness before it can return its trace.
        trace = [ToolCallHarnessEvent(kind="tool_result", observation=obs.model_copy(update={
            "payload": {key: value for key, value in obs.payload.items()
                        if key not in {"read_result", "read_results"}}
        })) for obs in policy.observations] if policy else []
        issues, _ = _review_execution_facts(trace, request)
        return issues

    async def run():
        nonlocal policy, phase, result
        reader = registry.get("reader") if "reader" in registry.names else None
        catalog = reader.material_catalog(request) if isinstance(reader, ReviewerReaderTool) else []
        policy = ReviewerReadingPolicy(registry, request, catalog=catalog,
            max_read_chars=reviewer.config.max_total_read_chars, max_tool_calls=reviewer.config.max_tool_calls)
        system = with_chinese_explanations(reviewer._render_instruction(reviewer.config.support_prompt_name, SUPPORT_INSTRUCTION))
        user = json.dumps({"novelty_point": request.novelty_point.model_dump(mode="json"),
            "tasks": [task.model_dump(mode="json") for task in request.tasks],
            "cards": [c.model_dump(mode="json") for c in request.cards],
            "evidence": reviewer_model_view([e.model_dump(mode="json") for e in request.evidence
                                               if e.evidence_id in request.cards[0].evidence_ids]),
            "feature_catalog": feature_catalog(request.novelty_point),
            "review_schema": schema}, ensure_ascii=False)
        _record_review_event("reviewer_case_start", request, {"actual_model_input": {"system": system, "user": user},
            "draft_schema": schema, "harness_state": state()})
        harness = ToolCallHarness(client, registry, config=ToolCallHarnessConfig(
            max_turns=reviewer.config.max_steps, max_tool_calls=reviewer.config.max_tool_calls,
            max_total_read_chars=reviewer.config.max_total_read_chars, text_choices=True),
            action_policy_factory=lambda _registry, _scope: policy)
        phase = "model"
        result = await harness.run(system_prompt=system, initial_user_message=user, scope=request, options=options)
        phase = "schema"
        try:
            draft = ReviewerSupportDraft.model_validate_json(_extract_json(result.final_content))
        except (ValueError, ValidationError) as exc:
            repaired = await repair_json(client, result.final_content,
                schema, options, validation_error=exc)
            draft = ReviewerSupportDraft.model_validate_json(_extract_json(repaired))
        phase = "reference"
        refs = [eid for work in draft.highly_relevant_works for eid in work.evidence_ids]
        refs += [eid for item in draft.feature_comparisons for eid in item.evidence_refs]
        if any(eid not in policy.ledger.choices() for eid in refs):
            raise ValueError("single-card draft cites evidence absent from its ledger")
        issues = execution_issues()
        # Validate references before marking the invocation completed.
        review = ReviewerSupportReview(**draft.model_dump(), status=ReviewStatus.INSUFFICIENT_EVIDENCE,
            support_status=policy.support_history[-1]["status"], harness_state=state(),
            review_evidence=list(policy.ledger.evidence.values()), reader_observations=list(policy.ledger.audits.values()),
            execution_issues=issues)
        validate_review_references(review, request)
        policy.complete()
        review = ReviewerSupportReview.model_validate({**review.model_dump(), "status": "reviewed",
                                                       "harness_state": state()})
        _record_review_event("single_card_result", request, {"review": review.model_dump(mode="json"),
            "raw_draft": draft.model_dump(mode="json"), "model_calls": client.calls_used,
            "execution_status": "completed"})
        return review

    try:
        return await asyncio.wait_for(run(), reviewer.config.card_timeout_seconds)
    except Exception as exc:
        if not reviewer.config.fail_closed:
            raise
        cause = ("budget_exhausted" if isinstance(exc, TimeoutError) and time.monotonic() >= client.deadline
                 else _review_failure_cause(exc))
        issues = execution_issues()
        review = ReviewerSupportReview(novelty_point_id=request.novelty_point.point_id,
            status=ReviewStatus.INSUFFICIENT_EVIDENCE, support_reason="单卡核验未完成；保留实际读取账本，需恢复核验。",
            incomplete_reason=cause, harness_state=state(),
            support_status=policy.support_history[-1]["status"] if policy and policy.support_history else None,
            review_evidence=list(policy.ledger.evidence.values()) if policy else [],
            reader_observations=list(policy.ledger.audits.values()) if policy else [],
            execution_issues=[*issues, _review_exception_issue(exc, request, phase)])
        _record_review_event("single_card_incomplete", request, {"review": review.model_dump(mode="json"),
            "error_type": type(exc).__name__, "error": str(exc), "model_calls": client.calls_used})
        return review
