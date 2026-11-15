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
from ..core.reviewer_choices import ReviewerSelectionError
from ..core.tool_call_harness import ToolCallHarness, ToolCallHarnessConfig, ToolCallHarnessEvent
from ..response_language import with_chinese_explanations
from ..schemas import ReviewStatus
from ..schemas.domain import ReviewerSupportReview
from ..tools.reader import ReviewerReaderTool
from ..tools.researcher_registry import ResearcherToolRegistry
from ..workflows.reviewer_reading_policy import ReviewerReadingPolicy


SUPPORT_INSTRUCTION = """你是单卡 Reviewer：核对一张参考文献卡的已读材料与目标技术特征的关系，不作点级新颖性裁定。

判断对象：novelty_point.claim 和 feature_catalog 是目标论文待查的主张；cards、参考 Work 和所选原文是比较材料。目标主张不能作为参考文献的事实，也不能用于证明自身。以 Harness 当前 judgment_menu 绑定的参考文献和特征为准。

按当前阶段行动：
- assess_card / reassess_card：选择材料可用性。可用表示有可追溯材料支持有限比较，不表示所有特征已披露，也不表示目标论文有或没有新颖性。存在局部依据与未知时保留两者。
- decide_reading：判断缺的是哪个特征的哪类依据。若当前只有目录、背景或概述，关键机制仍未知，且菜单有可读正文和预算，优先继续读取相关参考文献。单卡 Reader 按程序维护的位置顺序读取；不能自行跳到章节、指定位置或调用菜单之外的检索工具。
- 已有材料足以作有限判断，或继续读取不可用/不能解决缺口时，可选择结束并明确局限。不要求一律读完整篇；全文已保存、摘要已读完或章节标题已出现，都不代表相关正文已读。
- 菜单非空时只返回 {"option_id":"当前菜单中的选项"}。完成阅读后按新菜单重评；stage=draft 且菜单为空时才输出当前 review_schema 要求的最终 JSON。

逐槽位判断：阅读 judgment_menu 中的参考文献、目标特征和选项含义，先核对同一参考文献的证据，再选择关系。不要按选项编号顺序猜含义，不跨槽位复用判断。
- 披露：证据说明了该特征的关键机制和必要条件；仅出现术语或方法名称不够。
- 部分披露：证据确认了可指明的一部分机制，但仍有必要条件未确认；说明两者。
- 明确不符：证据明确描述同一对象、条件下与该特征不相容的机制或约束。另一种方法可能与目标方法共存，不能仅凭方法不同认定不符。
- 无法判断：未提及、未读到、仅有背景，或不足以确认机制。使用 unknown 对应选项及 insufficient 依据；可保留帮助说明局限的证据，也可不选证据。
例如，目标要求固定规模的新图，原文只介绍图嵌入或列出自编码器名称，不能据此确认固定规模，也不能判为明确不符。理由若只是“未详细描述，因此无法确认”，应选择无法判断；不能以推断为名把缺证据转成不符。

证据与解释：非 unknown 必须选同一参考文献、实际已展示且支持该关系的证据。依据类型按直接陈述、机制等价或有限推断选择；等价或推断要解释对应条件和限制。目录、导航、论文题名及登记成功本身不能证明技术关系。
attribution_choice 中的作者指参考文献作者；综述介绍的他人方法选 related_work 对应选项，无法确认归属则选 unknown 对应选项。参考文献提到某方法不表示它自己提出了该方法。
reason 简洁说明“所选原文确认的机制—与当前特征的对应或不相容条件—仍未知的部分”，不要复述目标贡献，不重复无关原文。输出前检查选项含义与理由一致；这项核对不另行输出字段。
material_assessment 概括当前卡片已读材料可用于哪些有限比较及其局限，material_evidence_choices 选择其依据。范围限定为“已读材料”，不从局部未提及推及整篇文献没有；有明确补证需求时填写 supplement_request，否则为 null。"""


SUPPORT_CHOICE_CONTRACT = """
单卡身份协议：最终只返回 material_assessment、material_evidence_choices、feature_judgments、supplement_request。
feature_judgments 每项只含 judgment_choice、basis_choice、attribution_choice、evidence_choices、reason。
feature_judgments 是按 judgment_menu 的 slot 键填写的对象，不是数组；每个槽位只用该组判断选项。
以上选项只能来自当前 Harness。不要输出 novelty_point_id、feature_id、work_choice、work_id、relation、basis_type、source_context、card_ids、evidence_refs、support_reason 或 highly_relevant_works；这些由程序绑定或组装。
判断选项的 supported 表示参考文献披露该特征，不表示目标论文主张已获证明。
关系、依据和理由必须一致：未提及、未读到、无法确认使用 unknown 对应选项；contradicted 须有同一对象、条件下不相容的原文依据。"""


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
    selection_draft = None
    client = _BudgetedClient(reviewer._client(), reviewer.config.card_timeout_seconds,
                             max_calls=reviewer.config.max_steps)
    options = replace(reviewer.model_options or ModelCallOptions(temperature=reviewer.config.temperature),
        tools=(), tool_choice="none", response_format=None)

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
        system = with_chinese_explanations(reviewer._render_instruction(reviewer.config.support_prompt_name, SUPPORT_INSTRUCTION) + SUPPORT_CHOICE_CONTRACT)
        schema = policy.draft_schema()
        card = request.cards[0].model_dump(mode="json")
        card["evidence_ids"] = [eid for eid in card["evidence_ids"] if eid in policy.ledger.choices()]
        user = json.dumps(policy.references.project({"novelty_point": request.novelty_point.model_dump(mode="json"),
            "tasks": [task.model_dump(mode="json") for task in request.tasks],
            "cards": [card],
            "evidence": reviewer_model_view([e.model_dump(mode="json") for e in request.evidence
                                               if e.evidence_id in policy.ledger.choices()]),
            "feature_catalog": feature_catalog(request.novelty_point)}), ensure_ascii=False)
        _record_review_event("reviewer_case_start", request, {"actual_model_input": {"system": system, "user": user},
            "draft_schema": schema, "harness_state": state()})
        harness = ToolCallHarness(client, registry, config=ToolCallHarnessConfig(
            max_turns=reviewer.config.max_steps, max_tool_calls=reviewer.config.max_tool_calls,
            max_total_read_chars=reviewer.config.max_total_read_chars, text_choices=True),
            action_policy_factory=lambda _registry, _scope: policy)
        phase = "model"
        result = await harness.run(system_prompt=system, initial_user_message=user, scope=request, options=options)
        phase = "schema"
        schema = policy.draft_schema()
        final_options = policy.final_options(options)

        def parse_draft(content):
            nonlocal selection_draft
            selection_draft = json.loads(_extract_json(content))
            return policy.judgments.compile(selection_draft)

        try:
            draft = parse_draft(result.final_content)
        except ReviewerSelectionError:
            raise
        except (ValueError, ValidationError) as exc:
            repaired = await repair_json(client, result.final_content,
                schema, final_options, validation_error=exc)
            draft = parse_draft(repaired)
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
            "model_selection": selection_draft,
            "judgment_menu": policy.judgments.menu(),
            "execution_status": "completed", "choice_bindings": policy.references.bindings})
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
            "error_type": type(exc).__name__, "error": str(exc), "model_calls": client.calls_used,
            "choice_bindings": policy.references.bindings if policy else {}})
        return review
