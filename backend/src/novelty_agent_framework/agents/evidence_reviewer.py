"""查新点级 Reviewer：综合 EvidenceCard/Evidence 并按需回读原文。"""

from __future__ import annotations

from ..response_language import with_chinese_explanations
from ..schemas.failures import failure_family, CodedFailure
from ..core.failure_classification import output_failure_code, model_failure_code

import json
import asyncio
import hashlib
import re
import time
from datetime import date, datetime, timezone
from collections.abc import Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from backend.env import ChatMessage, ModelCallBudgetExceeded, ModelCallOptions, ModelClient, ModelRegistry, PromptLibrary

from ..ports import EvidenceReviewer, ReviewResult
from ..core.format_repair import repair_json
from ..core.reviewer_evidence import ReviewerEvidenceRegistry, reviewer_model_view
from ..core import ToolCallHarness, ToolCallHarnessConfig
from ..core.tool_call_harness import ToolCallBudgetExhausted, ToolCallHarnessEvent
from ..core.review_validation import (
    feature_catalog as _feature_catalog,
    validate_review_references as _validate_review_references,
    has_complete_work_coverage,
)
from ..schemas import (
    EvidenceCard,
    EvidenceReviewDecision,
    EvidenceReviewIssue,
    IssueSeverity,
    NoveltyPoint,
    NoveltyPointReview,
    ReviewerCardDraft,
    ReviewerSummaryDraft,
    NoveltyPointReviewRequest,
    ReviewStatus,
    SupplementRequest,
    ResearchTask,
    ReviewVerdict,
)
from ..tools.researcher_registry import ResearcherToolRegistry
from ..tools.reader import ReviewerReaderTool
from ..schemas.domain import (ReviewEvidence, ReviewerReadAudit, ReviewerSupportDraft,
                              ReviewerSupportReview, parse_card_review)
from ..schemas.failures import FailureCode, FailureScope, FailureEvent, make_failure
from ..core.runtime_artifacts import current_runtime_artifacts


class ReviewDeadlineExceeded(TimeoutError):
    """The local single-card deadline expired."""


class _BudgetedClient:
    """Apply the remaining card budget to both async waiting and HTTP I/O."""

    def __init__(self, client, seconds, *, max_calls=None):
        self.client = client
        self.deadline = time.monotonic() + seconds
        self.remaining_calls = max_calls

    async def acomplete(self, messages, *, options=None):
        if self.remaining_calls is not None:
            if self.remaining_calls <= 0:
                raise ModelCallBudgetExceeded("Reviewer local model-call budget exhausted")
            self.remaining_calls -= 1
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise ReviewDeadlineExceeded("review deadline exhausted")
        options = options or ModelCallOptions()
        options = replace(options, timeout_seconds=min(options.timeout_seconds or 90, remaining))
        try:
            return await asyncio.wait_for(self.client.acomplete(messages, options=options), remaining)
        except TimeoutError as exc:
            if time.monotonic() >= self.deadline:
                raise ReviewDeadlineExceeded("review deadline exhausted") from exc
            raise


def _summary_remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise ReviewDeadlineExceeded("summary deadline exhausted before dispatch")
    return remaining


def _bound_review_schema(model, request, evidence_choices=None):
    """Harness-issued feature and evidence choices, shared with validation."""
    schema = model.model_json_schema()
    objects = [schema, *schema.get("anyOf", [])]
    for obj in objects:
        obj["properties"]["novelty_point_id"]["enum"] = [request.novelty_point.point_id]
    feature_ids = [item["feature_id"] for item in _feature_catalog(request.novelty_point)]
    if feature_ids:
        schema["$defs"]["FeatureComparison"]["properties"]["feature_id"]["enum"] = feature_ids
    else:
        for obj in objects:
            obj["properties"]["feature_comparisons"]["maxItems"] = 0
    if evidence_choices is None:
        evidence_choices = ReviewerEvidenceRegistry(request, []).choices()
    for definition, field in (("FeatureComparison", "evidence_refs"), ("RelevantWork", "evidence_ids")):
        refs = schema["$defs"][definition]["properties"][field]
        if evidence_choices:
            refs["items"]["enum"] = list(evidence_choices)
        else:
            refs["maxItems"] = 0
    return schema


def _review_response_format(model, schema):
    return {"type": "json_schema", "json_schema": {
        "name": model.__name__, "strict": True, "schema": schema}}


def _check_evidence_choices(draft, choices):
    selected = [eid for work in draft.highly_relevant_works for eid in work.evidence_ids]
    selected += [eid for item in draft.feature_comparisons for eid in item.evidence_refs]
    if any(eid not in choices for eid in selected):
        raise ValueError("Evidence selection is outside the current Harness choices; select only issued evidence_id values")
    return draft


class _ReviewerChoiceClient:
    """Refresh citation options before every draft, including budget finalization and repair."""

    def __init__(self, client, request, model, registry):
        self.client, self.request, self.model, self.registry = client, request, model, registry

    def __getattr__(self, name):
        return getattr(self.client, name)

    async def acomplete(self, messages, *, options=None):
        options = options or ModelCallOptions()
        if (options.response_format or {}).get("json_schema", {}).get("name") == "harness_decision":
            return await self.client.acomplete(messages, options=options)
        choices = self.registry.choices()
        schema = _bound_review_schema(self.model, self.request, choices)
        options = replace(options, response_format=_review_response_format(self.model, schema))
        messages = list(messages)
        messages[0] = replace(messages[0], content=(messages[0].content or "") +
            "\nHarness evidence choices (current; supersede earlier citation lists):\n" +
            json.dumps({"evidence_options": choices, "draft_schema": schema}, ensure_ascii=False) +
            "\n引用字段只能从 evidence_options 的键中选择；没有适用依据时保留空引用和判断局限。")
        return await self.client.acomplete(messages, options=options)

_ALLOWED_ISSUE_CODES = frozenset(
    {
        "unsupported_main_contribution",
        "unsupported_overlap",
        "unsupported_difference",
        "quote_not_supporting_claim",
        "scope_overstatement",
        "abstract_only_overclaim",
        "novelty_point_mismatch",
        "task_mismatch",
        "internal_contradiction",
        "confidence_overstated",
        "relevance_overstated",
        "missing_evidence_detail",
        "metadata_unverified",
        "fulltext_unavailable",
        "retrieval_coverage_unknown",
    }
)

_EVIDENCE_BOUNDARY = (
    "区分原文明确支持的事实与未核验的特征。摘要、局部片段或截短引文未提及某特征，"
    "不能证明文献未采用该特征；取得全文也不等于已读到关键段落。"
    "若最终裁定依赖尚未核验的关键差异，返回 status=insufficient_evidence，"
    "说明缺少的比较证据，不得仅降低 confidence 后继续裁定。"
    "保留已证实的局部重合；多篇分别覆盖部分特征不等于单篇公开完整组合。"
    "检索或获取失败、零命中、未展示证据均不能支持新颖性。"
    "不影响当前有限结论的次要未知可以作为局限保留。"
    "判断依据只能引用已提供的 evidence_id；读取原文由 Harness 登记为 Evidence。"
    "来源定位与运行记录由系统填写，不属于模型输出。"
)

_CARD_FALLBACK = (
    "本轮只核验一张 Card 对应的一篇论文，是中间结果，不作最终查新裁定。"
    "使用 ReviewerCardDraft 格式；verdict 仅描述该文献与查新点的关系。"
    "记录已覆盖特征、差异、引用可靠性和证据局限；部分相关文献也必须保留。"
    "优先使用输入 Evidence 原文，仅在具体疑问时回读。理由不超过300字。"
    "每项特征判断引用直接支持它的 evidence_id，说明机制、范围和归属。"
    "导航片段不充当技术依据；未提及属于未知，冲突须有同一条件下不相容的依据。"
)

_SUMMARY_FALLBACK = (
    "你是查新点级汇总 Reviewer。综合带索引的单卡核验结果和已核验关键引文，"
    "输出 ReviewerSummaryDraft。单卡结果是派生分析，不是原始证据。"
    "单卡失败、证据不足及 quote_truncated=true 的局限必须保留。"
    "本轮不可调用工具，不得用模型记忆填补缺口；只能引用输入已核验的 "
    "work_id、card_id 和 evidence_id。网页补充资料不得用于裁定。"
    "必要时返回 insufficient_evidence，并在 supplement_request 中列明待复核索引。"
    "只引用 key_quotes 中实际展示的非空片段，部分展示仅支持已展示范围。"
    "逐项核对特征判断与原文；单卡结论不能直接扩大为整体证明。"
    "理由简洁，只输出严格 JSON。"
)


@dataclass(frozen=True)
class EvidenceReviewerConfig:
    """Reviewer 运行配置。"""

    enabled: bool = False
    model_alias: str = "reviewer"
    temperature: float = 0.0
    max_cards_per_call: int = 8
    fail_closed: bool = True
    max_steps: int = 8
    max_tool_calls: int = 6
    max_total_read_chars: int = 32_000
    prompt_name: str = "reviewer/review_evidence"
    card_timeout_seconds: float = 240.0
    summary_timeout_seconds: float = 180.0
    summary_input_date: str | None = None
    # Direct construction retains the native baseline for frozen experiments.
    # Application factories explicitly enable the staged protocol.
    guided_reading: bool = False
    support_prompt: str = "reviewer/review_support"
    recover_summary_on_failure: bool = False

    def __post_init__(self) -> None:
        if not 0.0 <= self.temperature <= 2.0:
            raise ValueError("temperature 必须位于 0 到 2 之间")
        if self.max_cards_per_call < 1:
            raise ValueError("max_cards_per_call 必须至少为 1")
        if min(self.max_steps, self.max_tool_calls, self.max_total_read_chars) < 1:
            raise ValueError("reviewer harness budgets must be positive")
        if min(self.card_timeout_seconds, self.summary_timeout_seconds) <= 0:
            raise ValueError("reviewer timeouts must be positive")
        if not self.prompt_name.strip():
            raise ValueError("prompt_name 不能为空")
        if not self.support_prompt.strip():
            raise ValueError("support_prompt 不能为空")
        if self.guided_reading and self.max_steps < 2:
            raise ValueError("guided Reviewer requires a reserved final turn")
        if self.summary_input_date is not None:
            date.fromisoformat(self.summary_input_date)


class DemoEvidenceReviewer:
    """Null/Demo 实现：不伪造语义判断，显式返回证据不足。"""

    def review(
        self, request: NoveltyPointReviewRequest
    ) -> NoveltyPointReview:
        request = NoveltyPointReviewRequest.model_validate(request)
        return _insufficient_review(
            request.novelty_point.point_id,
            "Demo Reviewer 不执行新颖性语义判定。",
            cause="technical_error",
        )


class NoveltyEvidenceReviewer(EvidenceReviewer):
    """基于 LLM 与受限 Reader Harness 的查新点级 Reviewer。"""

    def __init__(
        self,
        model_client: ModelClient | None = None,
        *,
        prompts: PromptLibrary | None = None,
        models: ModelRegistry | None = None,
        config: EvidenceReviewerConfig | None = None,
        model_options: ModelCallOptions | None = None,
        tool_registry: ResearcherToolRegistry | None = None,
    ) -> None:
        self.model_client = model_client
        self._prompts = prompts
        self._models = models
        self.config = config or EvidenceReviewerConfig()
        self.model_options = model_options
        self.tools = tool_registry or ResearcherToolRegistry()
        self.harness = ToolCallHarness(
            self._client(),
            self.tools,
            config=ToolCallHarnessConfig(
                caller_role="reviewer",
                max_turns=self.config.max_steps,
                max_tool_calls=self.config.max_tool_calls,
                per_tool_limits={"reader": self.config.max_tool_calls},
                max_total_read_chars=self.config.max_total_read_chars,
            ),
        )

    def review(
        self,
        request: NoveltyPointReviewRequest | Sequence[EvidenceCard],
        *,
        points: Sequence[NoveltyPoint] = (),
        tasks: Sequence[ResearchTask] = (),
    ) -> Any:
        """评审一个查新点；旧的 cards 调用形状仅保留迁移兼容。"""

        if isinstance(request, NoveltyPointReviewRequest):
            return self._review_point(request)
        return self._review_cards_legacy(request, points=points, tasks=tasks)

    async def _review_point(
        self, request: NoveltyPointReviewRequest, *, card_only: bool = False
    ) -> NoveltyPointReview:
        request = NoveltyPointReviewRequest.model_validate(request)
        if not request.cards or not request.evidence:
            return _insufficient_review(
                request.novelty_point.point_id,
                "当前查新点没有可供可靠判定的已绑定证据。",
                cause="material_unavailable",
            )

        system, user = self._render_point_prompt(request)
        reader_tool = self.tools.get("reader") if "reader" in self.tools.names else None
        catalog = reader_tool.material_catalog(request) if isinstance(reader_tool, ReviewerReaderTool) else []
        evidence_registry = ReviewerEvidenceRegistry(request, catalog)
        user += "\n已授权材料目录（只可读取这些 Artifact）：\n" + json.dumps(reviewer_model_view(catalog), ensure_ascii=False)
        user += "\n固定特征 ID：\n" + json.dumps(_feature_catalog(request.novelty_point), ensure_ascii=False)
        if card_only:
            system += "\n" + self._render_instruction("reviewer/review_card", _CARD_FALLBACK)
        system += "\n" + _EVIDENCE_BOUNDARY
        phase = "model"
        try:
            client = _ReviewerChoiceClient(self._client(), request, ReviewerCardDraft, evidence_registry)
            harness = ToolCallHarness(client, self.tools, config=self.harness.config,
                observation_projector=evidence_registry.project_model_context)
            if card_only:
                client = _BudgetedClient(client, self.config.card_timeout_seconds)
                harness = ToolCallHarness(client, self.tools, config=replace(
                    self.harness.config, finalize_on_budget=True,
                    reserve_final_turn=True, report_budget=True,
                    allow_one_read_budget_correction=True,
                    finalization_instruction=(
                        "Reviewer 单卡预算收尾。不得再调用工具；仅使用已返回的 Reader 片段和原 Evidence。"
                        "按单卡 ReviewerCardDraft schema 输出严格 JSON；判断依据引用已提供的 evidence_id。"
                        "不得生成证据对象、定位信息、原文摘录或运行原因。"
                        "保留已支持的有限特征对应，未核验的关键项应为 unknown 或证据不足。"
                    ),
                ), observation_projector=evidence_registry.project_model_context)
            call_options = replace(self.model_options or ModelCallOptions(
                    temperature=self.config.temperature, tool_choice="auto"),
                response_format=None)
            system = with_chinese_explanations(system)
            _record_review_event("reviewer_case_start", request, {
                "material_catalog": catalog,
                "actual_model_input": {"system": system, "user": user},
                "draft_schema": ReviewerCardDraft.model_json_schema(),
                "draft_schema_sha256": hashlib.sha256(json.dumps(
                    ReviewerCardDraft.model_json_schema(), sort_keys=True).encode()).hexdigest(),
                "requested_budget": {"max_steps": self.config.max_steps,
                    "max_tool_calls": self.config.max_tool_calls,
                    "max_total_read_chars": self.config.max_total_read_chars,
                    "card_timeout_seconds": self.config.card_timeout_seconds},
                "effective_budget": {"max_turns": harness.config.max_turns,
                    "exploration_turns": harness.config.max_turns - int(harness.config.reserve_final_turn),
                    "max_tool_calls": harness.config.max_tool_calls,
                    "max_total_read_chars": harness.config.max_total_read_chars,
                    "finalization_reserved": harness.config.reserve_final_turn},
            })
            result = await harness.run(
                system_prompt=system,
                initial_user_message=user,
                scope=request,
                options=call_options,
            )
            phase = "schema"
            draft_schema = _bound_review_schema(ReviewerCardDraft, request, evidence_registry.choices())
            call_options = replace(call_options, response_format=_review_response_format(ReviewerCardDraft, draft_schema))
            try:
                draft = ReviewerCardDraft.model_validate_json(_extract_json(result.final_content))
                _check_evidence_choices(draft, evidence_registry.choices())
            except (ValidationError, ValueError) as parse_error:
                _record_review_event("model_output_contract_violation", request, {
                    "error": str(parse_error), "raw_model_output": result.final_content,
                    "draft_schema": draft_schema,
                })
                repaired = await repair_json(client, result.final_content,
                                             draft_schema, call_options, validation_error=parse_error)
                draft = ReviewerCardDraft.model_validate_json(_extract_json(repaired))
                _check_evidence_choices(draft, evidence_registry.choices())
            review = NoveltyPointReview.model_validate(draft.model_dump(mode="json"))
            execution_issues, read_audit = _review_execution_facts(result.trace, request)
            incomplete = "semantic_evidence" if review.status is ReviewStatus.INSUFFICIENT_EVIDENCE else None
            if incomplete and execution_issues:
                incomplete = ("material_unavailable" if all(failure_family(issue.code) == FailureCode.MATERIAL_UNAVAILABLE
                              for issue in execution_issues) else "technical_error")
            if review.status is ReviewStatus.REVIEWED:
                execution_issues = [issue.model_copy(update={"conclusion_effect": "limits_coverage"})
                                    for issue in execution_issues]
            review = review.model_copy(update={"incomplete_reason": incomplete,
                "execution_issues": execution_issues, "reader_observations": read_audit})
            phase = "reference"
            review = review.model_copy(update={"review_evidence": list(evidence_registry.evidence.values())})
            review = _validate_review_references(review, request)
            review = _validate_verdict_coverage(review, request)
            _record_review_event("single_card_result", request, {
                "material_catalog": catalog, "actual_model_input": {"system": system, "user": user},
                "draft_schema": ReviewerCardDraft.model_json_schema(),
                "raw_draft": draft.model_dump(mode="json"),
                "stop_reason": result.stop_reason,
                "execution_status": "completed", "finalization_attempted": result.stop_reason is not None,
                "read_ledger": _review_read_ledger(result.trace),
                "review": review.model_dump(mode="json"),
                "read_registration": [{"read_id": item.read_id,
                    "evidence_id": item.evidence_id, "status": "validated"}
                    for item in review.review_evidence],
                "raw_model_output": result.final_content,
                "legacy_guard_shadow": _legacy_guard_shadow(review, request),
            })
            return review
        except Exception as exc:
            _record_review_event("single_card_incomplete", request, {
                "error_type": type(exc).__name__, "error": str(exc),
                "cause": _review_failure_cause(exc),
                "execution_status": "incomplete", "stop_stage": "single_card",
                "read_ledger": _review_read_ledger(getattr(exc, "trace", ())),
                "trace": [str(item) for item in getattr(exc, "trace", ())],
            })
            if not self.config.fail_closed:
                raise
            return _insufficient_review(
                request.novelty_point.point_id,
                f"Reviewer 无法完成可靠判定：{type(exc).__name__}: {exc}"[:500],
                cause=_review_failure_cause(exc),
            ).model_copy(update={"execution_issues": [_review_exception_issue(exc, request, phase)]})

    async def review_card(self, request: NoveltyPointReviewRequest) -> NoveltyPointReview | ReviewerSupportReview:
        """Reuse the existing review contract for one indexed intermediate result."""
        if len(request.cards) != 1:
            raise ValueError("review_card requires exactly one card")
        if self.config.guided_reading:
            return await self._review_card_controlled(request)
        try:
            return await asyncio.wait_for(
                self._review_point(request, card_only=True), self.config.card_timeout_seconds
            )
        except TimeoutError as exc:
            return _insufficient_review(request.novelty_point.point_id, "单卡评审超过时间预算，核验未完成。", cause="budget_exhausted").model_copy(
                update={"execution_issues": [_review_exception_issue(exc, request, "model")]})

    def _summary_checkpoint_prompt(self) -> str:
        return (self._render_instruction("reviewer/summarize_reviews", _SUMMARY_FALLBACK)
                + "\n" + (self._adjudication_skill() if self.config.guided_reading else _EVIDENCE_BOUNDARY))

    @staticmethod
    def _adjudication_skill() -> str:
        path = Path(__file__).resolve().parents[1] / "skills/reviewer_information_adjudication/SKILL.md"
        return path.read_text(encoding="utf-8").split("---", 2)[-1].strip()

    async def _review_card_controlled(self, request):
        """Accept support selection, then ask about reading; never infer the choice."""
        from ..workflows.reviewer_reading_policy import ReviewerReadingPolicy
        request = NoveltyPointReviewRequest.model_validate(request)
        policy = None
        trace = ()
        phase = "model"
        try:
            reader = self.tools.get("reader") if "reader" in self.tools.names else None
            catalog = reader.material_catalog(request) if isinstance(reader, ReviewerReaderTool) else []
            policy = ReviewerReadingPolicy(self.tools, request, catalog=catalog,
                max_read_chars=self.config.max_total_read_chars, max_tool_calls=self.config.max_tool_calls)
            prompts = self._prompts or PromptLibrary(Path(__file__).resolve().parents[1] / "prompts")
            system = with_chinese_explanations(prompts.render(self.config.support_prompt).system
                                               + "\n" + self._adjudication_skill())
            draft_schema = _bound_review_schema(ReviewerSupportDraft, request)
            user = json.dumps(reviewer_model_view({"novelty_point": request.novelty_point.model_dump(mode="json"),
                "tasks": [t.model_dump(mode="json") for t in request.tasks],
                "card": request.cards[0].model_dump(mode="json"),
                "evidence": [e.model_dump(mode="json") for e in request.evidence
                             if e.evidence_id in request.cards[0].evidence_ids],
                "material_catalog": catalog, "feature_catalog": _feature_catalog(request.novelty_point),
                "draft_schema": draft_schema}), ensure_ascii=False)
            client = _BudgetedClient(self._client(), self.config.card_timeout_seconds,
                                     max_calls=self.config.max_steps)
            client = _ReviewerChoiceClient(client, request, ReviewerSupportDraft, policy.evidence_registry)
            options = replace(self.model_options or ModelCallOptions(temperature=self.config.temperature),
                              tools=(), tool_choice="none",
                              response_format=_review_response_format(ReviewerSupportDraft, draft_schema))
            harness = ToolCallHarness(client, self.tools, config=replace(self.harness.config,
                text_choices=True, finalize_on_budget=True, reserve_final_turn=True, report_budget=True,
                finalization_instruction=("阅读或选择预算结束。仅按 ReviewerSupportDraft 输出已有依据和未解决问题；"
                    "不得补写状态选择、verdict 或原文。执行是否完成由程序按已登记阶段判断。")),
                action_policy_factory=lambda registry, scope: policy)
            _record_review_event("reviewer_case_start", request, {"protocol": "reviewer_support_v1",
                "material_catalog": catalog, "actual_model_input": {"system": system, "user": user},
                "draft_schema": draft_schema})
            # The deadline includes Reader I/O and any output repair.
            async with asyncio.timeout(self.config.card_timeout_seconds):
                result = await harness.run(system_prompt=system, initial_user_message=user,
                                           scope=request, options=options)
                trace = result.trace
                phase = "schema"
                draft_schema = _bound_review_schema(ReviewerSupportDraft, request, policy.evidence_registry.choices())
                options = replace(options, response_format=_review_response_format(ReviewerSupportDraft, draft_schema))
                try:
                    draft = ReviewerSupportDraft.model_validate_json(_extract_json(result.final_content))
                    _check_evidence_choices(draft, policy.evidence_registry.choices())
                except (ValidationError, ValueError) as exc:
                    repaired = await repair_json(client, result.final_content,
                        draft_schema, options, validation_error=exc)
                    draft = ReviewerSupportDraft.model_validate_json(_extract_json(repaired))
                    _check_evidence_choices(draft, policy.evidence_registry.choices())
                execution_issues, read_audit = _review_execution_facts(trace, request)
                history = policy.support_history
                review = ReviewerSupportReview(**draft.model_dump(mode="json"),
                    status="reviewed" if policy.can_finish else "insufficient_evidence",
                    support_status=history[-1]["status"] if history else None,
                    harness_state={**policy.snapshot(), "read_receipts": [r for event in trace
                        if event.observation is not None
                        for r in event.observation.payload.get("read_receipts", [])]},
                    review_evidence=list(policy.evidence.values()),
                    incomplete_reason=None if policy.can_finish else "budget_exhausted",
                    execution_issues=execution_issues, reader_observations=read_audit)
                phase = "reference"
                review = _validate_review_references(review, request)
                _record_review_event("single_card_result", request, {"protocol": "reviewer_support_v1",
                    "review": review.model_dump(mode="json"), "raw_model_output": result.final_content,
                    "stop_reason": result.stop_reason, "harness_state": policy.snapshot(),
                    "read_ledger": _review_read_ledger(trace)})
                return review
        except Exception as exc:
            if not self.config.fail_closed:
                raise
            trace = getattr(exc, "trace", trace)
            if not trace and policy is not None:
                # An outer timeout cancels the loop before it can attach trace.
                # Preserve actual completed observations retained by the policy.
                trace = tuple(ToolCallHarnessEvent(kind="tool_result", observation=o)
                              for o in policy.observations)
            execution_issues, read_audit = _review_execution_facts(trace, request)
            cause = "budget_exhausted" if isinstance(exc, TimeoutError) else _review_failure_cause(exc)
            history = policy.support_history if policy else []
            review = ReviewerSupportReview(novelty_point_id=request.novelty_point.point_id,
                status="insufficient_evidence", support_reason="单卡核验未完成，已登记选择和读取事实保留。",
                support_status=history[-1]["status"] if history else None,
                harness_state={**(policy.snapshot() if policy else {}), "read_receipts": [r for event in trace
                    if event.observation is not None for r in event.observation.payload.get("read_receipts", [])]},
                review_evidence=list(policy.evidence.values()) if policy else [], incomplete_reason=cause,
                execution_issues=[*execution_issues, _review_exception_issue(exc, request, phase)],
                reader_observations=read_audit)
            _record_review_event("single_card_incomplete", request, {"protocol": "reviewer_support_v1",
                "error_type": type(exc).__name__, "error": str(exc),
                "review": review.model_dump(mode="json")})
            return review

    async def summarize_with_checkpoint(self, request, card_reviews, *, output_root, run_id):
        """Persist verified Card results before the existing summary call."""
        from .reviewer_checkpoint import ReviewerSummaryCheckpoint, ReviewSummaryAttempt
        deadline = time.monotonic() + self.config.summary_timeout_seconds
        try:
            checkpoint = ReviewerSummaryCheckpoint.create(self, request, card_reviews,
                output_root=output_root, run_id=run_id)
        except (OSError, ValueError, TypeError) as exc:
            # Legacy/unverifiable sources cannot authorize recovery. Preserve the
            # existing summary path and explicitly report that durability failed.
            review = await self.summarize_reviews(request, card_reviews)
            issue = make_failure(FailureCode.SUMMARY_CHECKPOINT_WRITE if isinstance(exc, OSError) else FailureCode.SUMMARY_CHECKPOINT_INVALID,
                scope=FailureScope(paper_id=request.subject_paper_id, run_id=run_id,
                                   point_id=request.novelty_point.point_id),
                message="Summary checkpoint unavailable: " + type(exc).__name__)
            return ReviewSummaryAttempt(attempt_id="uncheckpointed", status="checkpoint_unavailable",
                review=review, partial_card_results=card_reviews, execution_issues=[*review.execution_issues, issue])
        initial = await checkpoint.attempt(self, deadline=deadline)
        retryable = {FailureCode.MODEL_TIMEOUT, FailureCode.MODEL_TRANSPORT}
        if not (self.config.recover_summary_on_failure and initial.durable
                and initial.status == "failed" and initial.review.incomplete_reason == "technical_error"
                and initial.execution_issues and all(i.code in retryable for i in initial.execution_issues)
                and time.monotonic() < deadline):
            return initial
        # Restore and revalidate the same checkpoint; never rerun Card work or
        # reset the initial summary deadline/global model-call budget.
        try:
            recovered = await self.recover_summary(request, output_root=output_root, run_id=run_id,
                checkpoint_id=initial.checkpoint_id, _deadline=deadline)
        except (OSError, ValueError) as exc:
            issue = make_failure(FailureCode.SUMMARY_CHECKPOINT_INVALID,
                scope=FailureScope(paper_id=request.subject_paper_id, run_id=run_id,
                                   point_id=request.novelty_point.point_id),
                message="Automatic summary recovery rejected: " + type(exc).__name__)
            return initial.model_copy(update={"execution_issues": [*initial.execution_issues, issue]})
        _record_review_event("summary_recovery", request, {
            "initial": initial.model_dump(mode="json"), "recovery": recovered.model_dump(mode="json")})
        return recovered

    async def recover_summary(self, request, *, output_root, run_id, checkpoint_id, _deadline=None):
        """Explicit one-attempt summary recovery, with no retrieval or Reader tool call."""
        from .reviewer_checkpoint import ReviewerSummaryCheckpoint
        checkpoint = ReviewerSummaryCheckpoint.load(self, request, output_root=output_root,
            run_id=run_id, checkpoint_id=checkpoint_id)
        return await checkpoint.attempt(self, recovery=True, deadline=_deadline)

    async def summarize_reviews(self, request, card_reviews, *, _input_date=None, _deadline=None) -> NoveltyPointReview:
        """Synthesize compact card reviews; validate against originals locally."""
        deadline = _deadline if _deadline is not None else time.monotonic() + self.config.summary_timeout_seconds
        usable = [row for row in card_reviews if row.get("status") == "completed"
                  and row.get("review", {}).get("incomplete_reason") not in
                  {"technical_error", "budget_exhausted"}]
        if not usable:
            causes = [row.get("review", {}).get("incomplete_reason") for row in card_reviews]
            cause = "budget_exhausted" if causes and all(x == "budget_exhausted" for x in causes) else "technical_error"
            _record_review_event("summary_skipped", request, {
                "reason": "all_card_reviews_incomplete", "card_causes": causes})
            return _insufficient_review(request.novelty_point.point_id,
                "全部单卡核验因运行错误或预算限制未完成；未调用模型汇总。", cause=cause)
        system = self._summary_checkpoint_prompt()
        phase = "reference"
        try:
            rows, validated_ids = _compact_summary_rows(request, card_reviews)
            projected = _summary_model_rows(rows)
            evidence_choices = {quote["evidence_id"]: {"work_id": quote["work_id"], "card_id": row["card_id"]}
                                for row in projected for quote in row.get("key_quotes", [])
                                if quote["evidence_id"] in validated_ids}
            draft_schema = _bound_review_schema(ReviewerSummaryDraft, request, evidence_choices)
            user = json.dumps({
                "today": _input_date or self.config.summary_input_date or datetime.now(timezone.utc).date().isoformat(),
                "novelty_point": request.novelty_point.model_dump(mode="json"),
                "feature_catalog": _feature_catalog(request.novelty_point),
                "card_reviews": projected,
                "evidence_options": evidence_choices,
                "review_schema": draft_schema,
            }, ensure_ascii=False)
            lineage = _verify_summary_payload(rows, user)
            _record_review_event("summary_payload_prepared", request, {
                "sent_to_model": False, "payload_sha256": hashlib.sha256(user.encode()).hexdigest(),
                "evidence_lineage": lineage})
            remaining = _summary_remaining(deadline)
            transport_timeout = max(0.001, remaining - min(1.0, remaining * 0.1))
            summary_options = replace(self.model_options or ModelCallOptions(), tools=(), tool_choice="none",
                                      response_format=_review_response_format(ReviewerSummaryDraft, draft_schema),
                                      timeout_seconds=transport_timeout)
            _record_review_event("summary_client_entered", request, {
                "payload_sha256": hashlib.sha256(user.encode()).hexdigest(),
                "actual_model_input": user, "evidence_lineage": lineage,
                "remaining_seconds": remaining, "transport_timeout_seconds": transport_timeout})
            phase = "model"
            response = await asyncio.wait_for(self._client().acomplete(
                [ChatMessage(role="system", content=with_chinese_explanations(system)), ChatMessage(role="user", content=user)],
                options=summary_options,
            ), _summary_remaining(deadline))
            _record_review_event("summary_response_received", request, {
                "payload_sha256": hashlib.sha256(user.encode()).hexdigest(),
                "raw_model_output": response.content})
            if response.tool_calls:
                raise ValueError("summary attempted a tool call")
            phase = "schema"
            try:
                draft = ReviewerSummaryDraft.model_validate_json(_extract_json(response.content))
                _check_evidence_choices(draft, evidence_choices)
            except (ValidationError, ValueError) as parse_error:
                _record_review_event("model_output_contract_violation", request, {
                    "stage": "summary", "error": str(parse_error),
                    "raw_model_output": response.content,
                    "draft_schema": draft_schema,
                })
                remaining = _summary_remaining(deadline)
                repair_timeout = max(0.001, remaining - min(1.0, remaining * 0.1))
                repaired = await asyncio.wait_for(repair_json(
                    self._client(), response.content, draft_schema,
                    replace(summary_options, timeout_seconds=repair_timeout),
                    validation_error=parse_error),
                    _summary_remaining(deadline))
                draft = ReviewerSummaryDraft.model_validate_json(_extract_json(repaired))
                _check_evidence_choices(draft, evidence_choices)
            review = NoveltyPointReview.model_validate(draft.model_dump(mode="json"))
            review = review.model_copy(update={"incomplete_reason":
                _summary_incomplete_reason(rows) if review.status is ReviewStatus.INSUFFICIENT_EVIDENCE else None})
            phase = "reference"
            incremental = [ReviewEvidence.model_validate(item) for row in rows
                           for item in row.get("review_evidence", [])]
            inherited_issues = {issue.event_id: issue for row in card_reviews
                if row.get("status") == "completed"
                for issue in parse_card_review(row["review"]).execution_issues}
            if review.status is ReviewStatus.REVIEWED:
                inherited_issues = {key: issue.model_copy(update={"conclusion_effect": "limits_coverage"})
                                    for key, issue in inherited_issues.items()}
            observations = {}
            for row in rows:
                for item in row.get("review", {}).get("reader_observations", []):
                    key = item["read_id"]
                    if key in observations and observations[key] != item:
                        raise ValueError("conflicting Reader observations in summary")
                    observations[key] = item
            review = review.model_copy(update={"review_evidence": incremental,
                                               "reader_observations": [ReviewerReadAudit.model_validate(x)
                                                                       for x in observations.values()],
                                               "execution_issues": list(inherited_issues.values())})
            summary_refs = [eid for work in review.highly_relevant_works for eid in work.evidence_ids]
            summary_refs += [eid for item in review.feature_comparisons for eid in item.evidence_refs]
            if any(evidence_id not in validated_ids for evidence_id in summary_refs):
                raise ValueError("summary cites evidence not verified in card stage or without a displayed quote")
            review = _validate_review_references(review, request)
            review = _validate_verdict_coverage(review, request)
            _summary_remaining(deadline)
            _record_review_event("summary_result", request, {
                "rows": rows, "actual_model_input": user,
                "draft_schema": draft_schema,
                "raw_draft": draft.model_dump(mode="json"),
                "raw_model_output": response.content, "review": review.model_dump(mode="json"),
                "legacy_guard_shadow": _legacy_guard_shadow(review, request),
            })
            _summary_remaining(deadline)
            return review
        except Exception as exc:
            if not self.config.fail_closed:
                raise
            cause = ("budget_exhausted" if isinstance(exc, TimeoutError)
                     and time.monotonic() >= deadline else _review_failure_cause(exc))
            _record_review_event("summary_fallback_returned", request, {
                "reason": cause, "error_type": type(exc).__name__,
                "error": str(exc), "failure_phase": phase,
                "response_received": "response" in locals(),
                "model_draft_valid": "draft" in locals()})
            return _insufficient_review(
                request.novelty_point.point_id,
                f"Reviewer 汇总失败：{type(exc).__name__}: {exc}"[:500],
                cause=cause,
            ).model_copy(update={"execution_issues": [_review_exception_issue(exc, request, phase)]})
    def _render_point_prompt(
        self, request: NoveltyPointReviewRequest
    ) -> tuple[str, str]:
        variables = {
            "today": datetime.now(timezone.utc).date().isoformat(),
            "novelty_point_json": json.dumps(
                request.novelty_point.model_dump(mode="json"), ensure_ascii=False
            ),
            "tasks_json": json.dumps(
                [item.model_dump(mode="json") for item in request.tasks],
                ensure_ascii=False,
            ),
            "cards_json": json.dumps(
                [item.model_dump(mode="json") for item in request.cards],
                ensure_ascii=False,
            ),
            "evidence_json": json.dumps(
                reviewer_model_view([item.model_dump(mode="json") for item in request.evidence]),
                ensure_ascii=False,
            ),
            "review_schema": json.dumps(
                ReviewerCardDraft.model_json_schema(), ensure_ascii=False
            ),
        }
        if self._prompts is not None:
            rendered = self._prompts.render(
                self.config.prompt_name, **variables
            )
            return rendered.system, rendered.user
        return _fallback_point_system_prompt(), "\n".join(
            f"{key}: {value}" for key, value in variables.items()
        )

    def _render_instruction(self, name: str, fallback: str) -> str:
        if self._prompts is None:
            return fallback
        return self._prompts.render(name).system

    def _review_cards_legacy(
        self,
        cards: Sequence[EvidenceCard],
        *,
        points: Sequence[NoveltyPoint],
        tasks: Sequence[ResearchTask],
    ) -> ReviewResult:
        """临时兼容旧调用方；正式 Workflow 不再使用逐卡过滤结果。"""
        if not cards:
            return ReviewResult(accepted=(), rejected=(), needs_more=(), decisions=())

        self._client()
        valid_card_ids = frozenset(card.card_id for card in cards)
        decisions: list[EvidenceReviewDecision] = []
        failures: list[tuple[str, str]] = []

        for batch in _chunked(cards, self.config.max_cards_per_call):
            try:
                raw = self._call_model(batch, points, tasks)
                parsed = self._parse_decisions(raw, valid_card_ids)
            except Exception as exc:
                reason = f"review_failed: {exc}"
                for card in batch:
                    failures.append((card.card_id, reason))
                    decisions.append(
                        _failure_decision(card.card_id, reason, self.config.fail_closed)
                    )
                continue
            decisions.extend(parsed.decisions)
            failures.extend(parsed.failures)

        accepted, rejected, needs_more = self._apply_verdict(cards, decisions)
        return ReviewResult(
            accepted=tuple(accepted),
            rejected=tuple(rejected),
            needs_more=tuple(needs_more),
            decisions=tuple(decisions),
        )

    def _call_model(
        self,
        cards: Sequence[EvidenceCard],
        points: Sequence[NoveltyPoint],
        tasks: Sequence[ResearchTask],
    ) -> Any:
        today = datetime.now(timezone.utc).date().isoformat()
        payload = {
            "today": today,
            "points": [point.model_dump(mode="json") for point in points],
            "tasks": [task.model_dump(mode="json") for task in tasks],
            "cards": [card.model_dump(mode="json") for card in cards],
            "review_schema": _review_output_schema(),
        }
        variables = {
            "today": today,
            "points_json": json.dumps(payload["points"], ensure_ascii=False),
            "novelty_point_json": json.dumps(
                payload["points"][0] if payload["points"] else {},
                ensure_ascii=False,
            ),
            "tasks_json": json.dumps(payload["tasks"], ensure_ascii=False),
            "cards_json": json.dumps(payload["cards"], ensure_ascii=False),
            "evidence_json": "[]",
            "review_schema": json.dumps(payload["review_schema"], ensure_ascii=False),
        }
        if self._prompts is not None:
            rendered = self._prompts.render(self.config.prompt_name, **variables)
            system, user = rendered.system, rendered.user
        else:
            system = _fallback_system_prompt()
            user = (
                "请审查以下 EvidenceCard 列表，逐卡输出结构化决定。\n"
                f"当前可信日期（UTC）：{today}\n"
                "该日期是判断来源可核验性的唯一时间基准；不得使用模型内部日期，"
                "也不得仅凭 arXiv 编号的月份推断来源不可能存在。\n\n"
                f"输入数据：\n{json.dumps(payload, ensure_ascii=False)}"
            )
        response = self._client().complete(
            [
                ChatMessage(role="system", content=with_chinese_explanations(system)),
                ChatMessage(role="user", content=user),
            ],
            options=self.model_options
            or ModelCallOptions(temperature=self.config.temperature),
        )
        try:
            return json.loads(response.content)
        except json.JSONDecodeError as exc:
            raise ValueError("NoveltyEvidenceReviewer 返回内容不是合法 JSON") from exc

    def _parse_decisions(self, raw: Any, valid_card_ids: frozenset[str]) -> "_ParsedBatch":
        items = _normalize_decision_list(raw)
        decisions: list[EvidenceReviewDecision] = []
        failures: list[tuple[str, str]] = []
        for item in items:
            try:
                decision = EvidenceReviewDecision.model_validate(item)
            except ValidationError:
                continue
            if decision.card_id not in valid_card_ids:
                continue
            decisions.append(_sanitize_issues(decision))
        return _ParsedBatch(decisions=decisions, failures=failures)

    def _apply_verdict(
        self,
        cards: Sequence[EvidenceCard],
        decisions: Sequence[EvidenceReviewDecision],
    ) -> tuple[list[EvidenceCard], list[tuple[str, str]], list[str]]:
        decision_by_id = {decision.card_id: decision for decision in decisions}
        accepted: list[EvidenceCard] = []
        rejected: list[tuple[str, str]] = []
        needs_more: list[str] = []
        for card in cards:
            decision = decision_by_id.get(card.card_id)
            if decision is None:
                if self.config.fail_closed:
                    rejected.append((card.card_id, "review_missing_decision"))
                else:
                    accepted.append(card)
                continue
            if decision.verdict is ReviewVerdict.ACCEPT:
                accepted.append(card)
            elif decision.verdict is ReviewVerdict.REJECT:
                rejected.append((card.card_id, _summarize_decision(decision)))
            else:
                needs_more.append(card.card_id)
        return accepted, rejected, needs_more

    def _client(self) -> ModelClient:
        if self.model_client is not None:
            return self.model_client
        if self._models is not None:
            return self._models.client_for(self.config.model_alias)
        raise NotImplementedError("NoveltyEvidenceReviewer 需要注入 ModelClient 或 ModelRegistry")


@dataclass(frozen=True)
class _ParsedBatch:
    decisions: list[EvidenceReviewDecision]
    failures: list[tuple[str, str]]


def _chunked(cards: Sequence[EvidenceCard], size: int) -> list[list[EvidenceCard]]:
    if size <= 0:
        raise ValueError("chunk size 必须为正")
    return [list(cards[index : index + size]) for index in range(0, len(cards), size)]


def _normalize_decision_list(raw: Any) -> list[dict[str, Any]]:
    if isinstance(raw, dict):
        for key in ("decisions", "review_decisions"):
            if isinstance(raw.get(key), list):
                return [item for item in raw[key] if isinstance(item, dict)]
        if isinstance(raw.get("card_id"), str):
            return [raw]
        return []
    if isinstance(raw, list):
        return [item for item in raw if isinstance(item, dict)]
    return []


def _sanitize_issues(decision: EvidenceReviewDecision) -> EvidenceReviewDecision:
    if not decision.issues:
        return decision
    cleaned: list[EvidenceReviewIssue] = []
    for issue in decision.issues:
        if issue.code in _ALLOWED_ISSUE_CODES:
            cleaned.append(issue)
        else:
            cleaned.append(
                EvidenceReviewIssue(
                    code="missing_evidence_detail",
                    message=f"模型返回未知 issue.code '{issue.code}'：{issue.message}",
                    severity=IssueSeverity.WARNING,
                    field=issue.field,
                    source_index=issue.source_index,
                )
            )
    return decision.model_copy(update={"issues": cleaned})


def _summarize_decision(decision: EvidenceReviewDecision) -> str:
    if not decision.issues:
        return f"verdict={decision.verdict.value}"
    codes = ",".join(issue.code for issue in decision.issues)
    return f"{codes}: {decision.issues[0].message}"


def _failure_decision(card_id: str, reason: str, fail_closed: bool) -> EvidenceReviewDecision:
    if fail_closed:
        return EvidenceReviewDecision(
            card_id=card_id,
            verdict=ReviewVerdict.REJECT,
            issues=[EvidenceReviewIssue(code="missing_evidence_detail", message=reason, severity=IssueSeverity.ERROR)],
            reviewed_confidence=0.0,
        )
    return EvidenceReviewDecision(
        card_id=card_id,
        verdict=ReviewVerdict.ACCEPT,
        issues=[EvidenceReviewIssue(code="missing_evidence_detail", message=f"fail_open 放行（{reason}）", severity=IssueSeverity.WARNING)],
        reviewed_confidence=0.0,
    )


def _review_output_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "required": ["decisions"],
        "properties": {"decisions": {"type": "array", "items": EvidenceReviewDecision.model_json_schema()}},
    }


def _fallback_system_prompt() -> str:
    return (
        "你是论文查新系统的证据审查 Agent。你只能依据输入内容判断 EvidenceCard 的"
        "语义和证据一致性，不能修改 EvidenceCard，不能创造新的 DOI/URL/引文/相同点/"
        "不同点，不能使用模型记忆补充论文事实。无法判断时输出 needs_more_evidence，"
        "不能猜测。输出必须是 JSON：{\"decisions\": [EvidenceReviewDecision, ...]}。"
        "禁止输出开场白、解释性 Markdown 或自由文本。"
    )


def _fallback_point_system_prompt() -> str:
    return (
        "你是查新点级信息判定 Agent。只依据输入 EvidenceCard、Evidence 和 reader "
        "回读结果综合多个 Work；不得搜索或使用模型记忆补充事实。证据充分时输出 "
        "NoveltyPointReview；证据不足时输出 insufficient_evidence，不能猜测。"
        "仅使用论文 Evidence；source_kind=web_supplement 或旧 web_supplement_evidence "
        "仅为补充资料，不得用于裁定。LLM summary 不能作为原始 Evidence。"
    )


def _extract_json(content: str | None) -> str:
    text = (content or "").strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    start, end = text.find("{"), text.rfind("}")
    return text[start : end + 1] if start != -1 and end > start else text


def _review_execution_facts(trace, request):
    issues, reads = {}, {}
    for index, event in enumerate(trace):
        observation = getattr(event, "observation", None)
        if getattr(event, "kind", None) != "tool_result" or observation is None:
            continue
        payload = observation.payload
        raw_issues = payload.get("execution_issues", [])
        for raw in raw_issues:
            issue = FailureEvent.model_validate(raw)
            issues[issue.event_id] = issue
        errors = payload.get("read_errors", [])
        if not observation.succeeded and not raw_issues and not errors:
            errors = [{"error_type": payload.get("error_type"),
                       "artifact_id": observation.arguments.get("artifact_id")}]
        for error in errors:
            # Structured exception identity, never the prose error message.
            if error.get("error_type") == "ReaderUnavailable" and raw_issues:
                continue
            code = {"PermissionError": FailureCode.TOOL_SCOPE,
                    "ValidationError": FailureCode.TOOL_ARGUMENTS,
                    "ValueError": FailureCode.TOOL_ARGUMENTS,
                    "FileNotFoundError": FailureCode.MATERIAL_UNAVAILABLE}.get(
                        error.get("error_type"), FailureCode.TOOL_UNAVAILABLE)
            issue = make_failure(code, scope=FailureScope(paper_id=request.subject_paper_id,
                point_id=request.novelty_point.point_id, artifact_id=error.get("artifact_id")),
                message="Reviewer tool execution did not complete", occurrence_id=str(index))
            issues[issue.event_id] = issue
        if observation.tool_name != "reader" or not observation.succeeded:
            continue
        for raw in payload.get("read_results", []) + ([payload["read_result"]] if "read_result" in payload else []):
            audit = ReviewerReadAudit(read_id=raw["read_id"], namespace=raw["namespace"],
                work_id=raw["work_id"], artifact_id=raw["artifact_id"], artifact_hash=raw["sha256"],
                char_start=raw["char_start"], char_end=raw["char_end"],
                text_sha256=hashlib.sha256(raw["text"].encode()).hexdigest())
            reads[audit.read_id] = audit
    return list(issues.values()), list(reads.values())


def _review_exception_issue(exc, request, phase):
    from backend.env import ModelContextAdmissionError
    from backend.env.model_client import ModelTransportTimeout
    chain, current = [], exc
    while current is not None and id(current) not in {id(item) for item in chain}:
        chain.append(current)
        current = current.__cause__
    if any(isinstance(item, ModelContextAdmissionError) for item in chain):
        admission = next(item for item in chain if isinstance(item, ModelContextAdmissionError))
        code = (FailureCode.MODEL_CONTEXT_UNAVAILABLE if admission.code == "CONTEXT_MEASUREMENT_UNAVAILABLE"
                else FailureCode.MODEL_CONTEXT_LIMIT)
    elif any(isinstance(item, (TimeoutError, ModelTransportTimeout)) for item in chain):
        code = FailureCode.MODEL_TIMEOUT
    elif _review_failure_cause(exc) == "budget_exhausted":
        code = FailureCode.BUDGET_EXHAUSTED
    elif phase == "schema":
        code = output_failure_code(exc)
    elif phase == "reference":
        code = next((item.code for item in chain if isinstance(item, CodedFailure)), FailureCode.MODEL_OUTPUT_REFERENCE)
    else:
        code = next((model_failure_code(item) for item in chain
                     if model_failure_code(item) != FailureCode.UNKNOWN), FailureCode.UNKNOWN)
    return make_failure(code, scope=FailureScope(node=f"reviewer.{phase}", paper_id=request.subject_paper_id,
        point_id=request.novelty_point.point_id), message="Reviewer execution incomplete")


def _review_failure_cause(exc: BaseException) -> str:
    """Classify by exception identity, including a harness-wrapped cause."""
    current: BaseException | None = exc
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, (ToolCallBudgetExhausted, ModelCallBudgetExceeded, ReviewDeadlineExceeded)):
            return "budget_exhausted"
        current = current.__cause__
    return "technical_error"


def _review_read_ledger(trace) -> dict[str, Any]:
    ranges: dict[str, list[tuple[int, int]]] = {}
    returned = 0
    for event in trace:
        observation = getattr(event, "observation", None)
        if event.kind != "tool_result" or observation is None or observation.tool_name != "reader":
            continue
        payload = observation.payload
        reads = payload.get("read_results", []) + ([payload["read_result"]] if "read_result" in payload else [])
        for read in reads:
            start, end = read.get("char_start"), read.get("char_end")
            if not isinstance(start, int) or not isinstance(end, int) or end <= start:
                continue
            returned += end - start
            ranges.setdefault(read["artifact_id"], []).append((start, end))
    covered = 0
    for items in ranges.values():
        last_start = last_end = None
        for start, end in sorted(items):
            if last_end is None:
                last_start, last_end = start, end
            elif start <= last_end:
                last_end = max(last_end, end)
            else:
                covered += last_end - last_start
                last_start, last_end = start, end
        if last_end is not None:
            covered += last_end - last_start
    return {"returned_chars": returned, "unique_covered_chars": covered,
            "ranges": {artifact: [[start, end] for start, end in items]
                       for artifact, items in ranges.items()}}


def _insufficient_review(point_id: str, reason: str, *, cause: str = "semantic_evidence") -> NoveltyPointReview:
    return NoveltyPointReview(
        novelty_point_id=point_id,
        status=ReviewStatus.INSUFFICIENT_EVIDENCE,
        supplement_request=SupplementRequest(reason=reason),
        incomplete_reason=cause,
    )


_STRONG_ABSENCE = re.compile(
    r"未(?:使用|采用|涉及|包含|公开|披露|用)|不涉及|没有(?:使用|采用|包含)|"
    r"\b(?:does not use|doesn't use|does not include|does not involve|without)\b",
    re.IGNORECASE,
)
_NOVEL_ABSENCE = re.compile(r"未(?:覆盖|见|报告|呈现)|不包含|\b(?:not disclosed|not reported)\b", re.IGNORECASE)
_EXPLICIT_NEGATION = re.compile(
    r"未(?:使用|采用|包含|涉及)|不(?:使用|采用|包含|涉及)|"
    r"\b(?:not|without|no)\b", re.IGNORECASE,
)


def _guard_unsupported_absence(
    review: NoveltyPointReview, request: NoveltyPointReviewRequest
) -> NoveltyPointReview:
    """Fail closed on absence claims when no supplied quotation states an absence.

    This is a narrow syntactic guard, not a substitute for semantic review. It
    deliberately preserves the relevant works so partial overlap remains visible.
    """
    if review.status is not ReviewStatus.REVIEWED:
        return review
    assertions = "\n".join([
        review.verdict_reason or "",
        *(work.relevance_reason for work in review.highly_relevant_works),
    ])
    if not (_STRONG_ABSENCE.search(assertions) or
            (review.verdict is not None and review.verdict.value == "novel" and
             _NOVEL_ABSENCE.search(assertions))):
        return review
    if any(_EXPLICIT_NEGATION.search(item.quote) for item in request.evidence):
        return review
    return NoveltyPointReview(
        novelty_point_id=review.novelty_point_id,
        status=ReviewStatus.INSUFFICIENT_EVIDENCE,
        highly_relevant_works=[work.model_copy(update={
            "relevance_reason": "该文献与查新点存在局部相关性；具体技术差异尚待原文核验。",
        }) for work in review.highly_relevant_works],
        supplement_request=SupplementRequest(
            reason="裁定依赖文献未采用特征的断言，但输入引文没有直接支持该否定；需核验原文。",
            missing_aspects=["核验被声称未采用的关键特征"],
        ),
    )


def _legacy_guard_shadow(review, request) -> dict[str, Any]:
    """Report the old lexical decision without affecting the formal review."""
    previous = _guard_unsupported_absence(review, request)
    assertions = "\n".join([review.verdict_reason or "", *(
        work.relevance_reason for work in review.highly_relevant_works)])
    matches = [{"rule": name, "start": match.start(), "end": match.end(),
                "text": match.group()}
               for name, pattern in (("strong_absence", _STRONG_ABSENCE),
                                     ("novel_absence", _NOVEL_ABSENCE))
               for match in pattern.finditer(assertions)]
    quote_matches = [{"evidence_id": item.evidence_id, "start": match.start(),
                      "end": match.end(), "text": match.group()}
                     for item in request.evidence
                     for match in _EXPLICIT_NEGATION.finditer(item.quote)]
    return {"variant_id": "G0_shadow", "would_change": previous.status != review.status,
            "old_status": previous.status.value, "formal_status": review.status.value,
            "old_verdict": previous.verdict.value if previous.verdict else None,
            "old_relevance_reasons": [work.relevance_reason for work in previous.highly_relevant_works],
            "assertion_matches": matches, "input_quote_matches": quote_matches}


def _record_review_event(phase: str, request, payload: dict[str, Any]) -> None:
    runtime = current_runtime_artifacts()
    if runtime is not None:
        runtime.record_reviewer_event({"phase": phase,
            "point_id": request.novelty_point.point_id,
            "card_ids": [c.card_id for c in request.cards], **payload})




def _compact_summary_rows(request, card_reviews):
    """Carry cited original and registered Reviewer quotes into a bounded summary."""
    cards = {card.card_id: card for card in request.cards}
    evidence = {item.evidence_id: item for item in request.evidence}
    rows, validated_ids = [], set()
    for row in card_reviews:
        card = cards.get(row.get("card_id"))
        if card is None or row.get("novelty_point_id") != request.novelty_point.point_id:
            raise ValueError("indexed card review is outside request scope")
        compact = {key: row[key] for key in ("index", "card_id", "novelty_point_id", "status")}
        compact["document_title"] = card.document_title
        if row.get("status") != "completed":
            compact["error"] = str(row.get("error", "单卡核验未完成"))[:500]
            rows.append(compact)
            continue
        review = parse_card_review(row.get("review"))
        single = NoveltyPointReviewRequest(
            subject_paper_id=request.subject_paper_id,
            novelty_point=request.novelty_point, tasks=request.tasks, cards=[card],
            evidence=[item for item in request.evidence if item.evidence_id in card.evidence_ids],
        )
        _validate_review_references(review, single)
        ids = list(dict.fromkeys(
            eid for work in review.highly_relevant_works for eid in work.evidence_ids
        ))
        ids = list(dict.fromkeys(ids + [eid for item in review.feature_comparisons
                                        for eid in item.evidence_refs]))
        # Harness-registered reads remain available as background evidence;
        # registration alone never asserts support for a model judgment.
        ids = list(dict.fromkeys(ids + [item.evidence_id for item in review.review_evidence]))
        if isinstance(review, ReviewerSupportReview):
            # Initial usability may rely only on the supplied Card materials.
            # Carry those original quotes even without a repeated draft citation.
            ids = list(dict.fromkeys(ids + card.evidence_ids))
        compact["review"] = review.model_dump(mode="json")
        additions = {item.evidence_id: item for item in review.review_evidence}
        # Explicit argument references take precedence over selected background reads.
        cited = {eid for work in review.highly_relevant_works for eid in work.evidence_ids}
        cited.update(eid for item in review.feature_comparisons for eid in item.evidence_refs)
        ids.sort(key=lambda eid: eid not in cited)
        compact["review_evidence"] = [item.model_dump(mode="json") for item in review.review_evidence]
        budget = 12_000
        quotes = []
        for position, eid in enumerate(ids):
            item = additions.get(eid) or evidence.get(eid)
            if item is None:
                raise ValueError("summary cites an unknown evidence_id")
            quote = item.exact_quote if isinstance(item, ReviewEvidence) else item.quote
            # Give each selected source a visible slice in the new protocol;
            # one long background read must not hide all later feature evidence.
            allowance = budget // (len(ids) - position) if isinstance(review, ReviewerSupportReview) else budget
            shown = quote[:allowance]
            status = "omitted" if not shown else "full" if len(shown) == len(quote) else "partial"
            payload = {"evidence_id": eid, "work_id": item.work_id,
                "artifact_id": item.artifact_id, "quote": shown,
                "quote_truncated": status != "full", "display_status": status,
                "displayed_chars": len(shown), "original_chars": len(quote),
                "quote_sha256": hashlib.sha256(quote.encode()).hexdigest(),
                "displayed_sha256": hashlib.sha256(shown.encode()).hexdigest(),
                "selected": eid in additions, "cited_by_card": eid in cited,
                "semantic_support": "unassessed"}
            for field in ("source_record_id", "role", "content_extent", "version_label",
                          "char_start", "char_end", "read_id", "artifact_hash"):
                if hasattr(item, field):
                    payload[field] = getattr(item, field)
            quotes.append(payload)
            if shown:
                validated_ids.add(eid)
            budget -= len(shown)
        compact["key_quotes"] = quotes
        compact["omitted_quote_count"] = sum(not item["quote"] for item in quotes)
        compact["summary_input_complete"] = all(not item["quote_truncated"] for item in quotes)
        rows.append(compact)
    return rows, validated_ids


def _summary_model_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Send each exact quote once, through the bounded key_quotes projection."""
    projected = []
    for row in rows:
        item = {key: value for key, value in row.items() if key != "review_evidence"}
        if "review" in item:
            item["review"] = {key: value for key, value in item["review"].items()
                              if key not in {"review_evidence", "read_citations", "reader_observations"}}
        projected.append(reviewer_model_view(item))
    return projected


def _verify_summary_payload(rows: list[dict[str, Any]], user: str) -> list[dict[str, Any]]:
    """Check the serialized user message, the last local boundary before dispatch."""
    actual = json.loads(user)["card_reviews"]
    if len(actual) != len(rows):
        raise ValueError("summary payload lost a card review")
    lineage = []
    for original, sent in zip(rows, actual):
        if original["card_id"] != sent["card_id"]:
            raise ValueError("summary payload changed card order")
        expected = original.get("key_quotes", [])
        displayed = sent.get("key_quotes", [])
        if len(expected) != len(displayed):
            raise ValueError("summary payload lost selected evidence")
        for source, item in zip(expected, displayed):
            if reviewer_model_view(source) != item or hashlib.sha256(item["quote"].encode()).hexdigest() != item["displayed_sha256"]:
                raise ValueError("summary payload changed a displayed quote")
            lineage.append({"card_id": sent["card_id"], **source})
    return lineage


def _summary_incomplete_reason(rows: list[dict[str, Any]]) -> str:
    reasons = [row.get("review", {}).get("incomplete_reason")
               for row in rows if row.get("status") == "completed"]
    if any(row.get("status") == "failed" for row in rows):
        reasons.append("technical_error")
    for cause in ("budget_exhausted", "technical_error", "material_unavailable"):
        if cause in reasons:
            return cause
    return "semantic_evidence"


def _validate_verdict_coverage(review, request):
    """A not_novel label requires one Work covering every declared feature.

    This checks the model's own structured relations, not their semantic truth.
    A secondary unknown must not automatically veto a limited/partial judgment.
    """
    if review.status is not ReviewStatus.REVIEWED or review.verdict is None or review.verdict.value != "not_novel":
        return review
    features = _feature_catalog(request.novelty_point)
    expected = {item["feature_id"] for item in features}
    by_work = {}
    for comparison in review.feature_comparisons:
        by_work.setdefault(comparison.work_id, {}).setdefault(comparison.feature_id, []).append(comparison)
    if has_complete_work_coverage(review, request.novelty_point):
        return review
    issue = make_failure(FailureCode.REVIEW_MISSING_FEATURES,
        scope=FailureScope(paper_id=request.subject_paper_id,
            point_id=request.novelty_point.point_id, feature_ids=sorted(expected)),
        message="not_novel requires one Work with a unique supported, cited comparison for every declared feature")
    _record_review_event("verdict_coverage_rejected", request, {
        "review_before_guard": review.model_dump(mode="json"),
        "required_feature_ids": sorted(expected), "failure": issue.model_dump(mode="json"),
        "semantic_relations_rejudged": False})
    missing = [item["text"] for item in features if not any(
        len(comparisons.get(item["feature_id"], [])) == 1
        and comparisons[item["feature_id"]][0].relation == "supported"
        and comparisons[item["feature_id"]][0].evidence_refs for comparisons in by_work.values())]
    return review.model_copy(update={"status": ReviewStatus.INSUFFICIENT_EVIDENCE,
        "verdict": None, "verdict_reason": None, "confidence": None,
        "incomplete_reason": "semantic_evidence",
        "supplement_request": SupplementRequest(
            reason="尚无单篇文献对全部明确技术特征的完整支持，不能形成 not_novel 裁定；已登记局部核验结果保留。",
            missing_aspects=missing or ["单篇文献对完整技术组合的证据对应"]),
        "execution_issues": [*review.execution_issues, issue]})
