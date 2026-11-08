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
from typing import Any

from pydantic import ValidationError

from backend.env import ChatMessage, ModelCallBudgetExceeded, ModelCallOptions, ModelClient, ModelRegistry, PromptLibrary

from ..ports import EvidenceReviewer, ReviewResult
from ..core.format_repair import repair_json
from ..core import ToolCallHarness, ToolCallHarnessConfig
from ..core.tool_call_harness import ToolCallBudgetExhausted
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
from ..schemas.domain import ReviewEvidence, ReviewerReadAudit, parse_card_review, ReviewerSupportReview
from ..schemas.failures import FailureCode, FailureScope, FailureEvent, make_failure
from ..core.runtime_artifacts import current_runtime_artifacts
from ..core.reviewer_summary import card_handoff, SummaryVerification
from ..schemas.reviewer_handoff import CardSummaryHandoff, SummaryRecheckRequest


class ReviewDeadlineExceeded(TimeoutError):
    """The local single-card deadline expired."""


class _BudgetedClient:
    """Apply the remaining card budget to both async waiting and HTTP I/O."""

    def __init__(self, client, seconds, *, max_calls=None):
        self.client = client
        self.deadline = time.monotonic() + seconds
        self.max_calls, self.calls_used = max_calls, 0

    async def acomplete(self, messages, *, options=None):
        if self.max_calls is not None and self.calls_used >= self.max_calls:
            raise ToolCallBudgetExhausted("single-card model call budget exhausted")
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise ReviewDeadlineExceeded("review deadline exhausted")
        options = options or ModelCallOptions()
        options = replace(options, timeout_seconds=min(options.timeout_seconds or 90, remaining))
        try:
            self.calls_used += 1
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
    "read_citations 只能填写本轮 reader 工具实际返回的 read_id；本轮未调用 reader 时"
    "必须输出空数组，不得复制输入 Evidence.provenance.read_id。"
)

_CARD_FALLBACK = (
    "本轮只核验一张 Card 对应的一篇论文，是中间结果，不作最终查新裁定。"
    "使用 ReviewerCardDraft 格式；verdict 仅描述该文献与查新点的关系。"
    "记录已覆盖特征、差异、引用可靠性和证据局限；部分相关文献也必须保留。"
    "优先使用输入 Evidence 原文，仅在具体疑问时回读。理由不超过300字。"
    "每项特征判断引用直接支持它的 Evidence ID 或真实 read_id，说明机制、范围和归属。"
    "导航片段不充当技术依据；未提及属于未知，冲突须有同一条件下不相容的依据。"
)

_SUMMARY_FALLBACK = (
    "你是查新点级 Reviewer，主要综合单卡判断，必要时才定向复核，不默认重读原文。"
    "card_reviews 是 card_judgment_v1 交接，selected_evidence 仅列单卡选中的证据身份，"
    "原文和阅读账本保留在数据面。未在本轮重复展示不否定已承接的单卡依据。"
    "默认输出 ReviewerSummaryDraft，承接的 feature_comparisons 逐字段原样保留，"
    "在点级理由中组织跨文献综合，不机械投票。区分单篇完整组合与多篇分别覆盖部分特征。"
    "发现判断冲突、理由与判断不一致或关键依据不明确，输出 recheck_schema 所示对象，"
    "指定 card_id、work_id、feature_id、该卡 selected_evidence 中的 evidence_ids 和具体 question。"
    "程序只返回这些已留存证据的原文，不获取新材料、不重新评审整张卡。"
    "修改或新增非 unknown 特征判断须先定向复核，引用该特征本轮展示的证据；"
    "不能将其他特征的引用移作新的依据。没有足够依据时保留 unknown 和局限，"
    "必要时返回 insufficient_evidence 并填写 supplement_request，不用模型记忆补缺口。"
    "单卡未完成不能作为已确认依据；登记和身份核验不等于语义正确。"
    "不得输出工具调用或自行生成证据、读取位置。只输出一个最终草稿或一个复核请求 JSON。"
)

# Always append the protocol even when a deployment supplies a custom prompt.
_SUMMARY_CONTRACT = (
    "\n交接契约 card_judgment_v1：默认没有 key_quotes 或阅读账本；"
    "承接单卡已选依据不要求再次展示原文。保持承接特征比较的所有字段不变；"
    "新增、修改或冲突裁决先输出 recheck_schema 请求，不直接扩大单卡判断。"
    "摘要、局部片段或截短引文未提及某特征，不能证明文献未采用该特征。"
    "单卡 unknown、未提及或材料缺失不能被写成已证实差异，检索失败和零命中不能证明新颖性。"
    "若裁定依赖尚未核验的关键差异，返回 status=insufficient_evidence，不仅降低 confidence。"
)


@dataclass(frozen=True)
class EvidenceReviewerConfig:
    """Reviewer 运行配置。"""

    enabled: bool = False
    controlled_reading: bool = True
    model_alias: str = "reviewer"
    temperature: float = 0.0
    max_cards_per_call: int = 8
    fail_closed: bool = True
    max_steps: int = 8
    max_tool_calls: int = 6
    max_total_read_chars: int = 32_000
    prompt_name: str = "reviewer/review_evidence"
    support_prompt_name: str = "reviewer/review_support"
    card_timeout_seconds: float = 240.0
    summary_timeout_seconds: float = 180.0
    summary_input_date: str | None = None
    summary_max_calls: int = 6
    summary_max_rechecks: int = 2
    summary_max_recheck_chars: int = 32_000

    def __post_init__(self) -> None:
        if not 0.0 <= self.temperature <= 2.0:
            raise ValueError("temperature 必须位于 0 到 2 之间")
        if self.max_cards_per_call < 1:
            raise ValueError("max_cards_per_call 必须至少为 1")
        if min(self.max_steps, self.max_tool_calls, self.max_total_read_chars) < 1:
            raise ValueError("reviewer harness budgets must be positive")
        if min(self.card_timeout_seconds, self.summary_timeout_seconds) <= 0:
            raise ValueError("reviewer timeouts must be positive")
        if min(self.summary_max_calls, self.summary_max_rechecks, self.summary_max_recheck_chars) < 1:
            raise ValueError("summary verification budgets must be positive")
        if not self.prompt_name.strip():
            raise ValueError("prompt_name 不能为空")
        if not self.support_prompt_name.strip():
            raise ValueError("support_prompt_name 不能为空")
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
        user += "\n已授权材料目录（只可读取这些 Artifact）：\n" + json.dumps(catalog, ensure_ascii=False)
        user += "\n固定特征 ID：\n" + json.dumps(_feature_catalog(request.novelty_point), ensure_ascii=False)
        if card_only:
            system += "\n" + self._render_instruction("reviewer/review_card", _CARD_FALLBACK)
        system += "\n" + _EVIDENCE_BOUNDARY
        phase = "model"
        try:
            harness = self.harness
            client = self._client()
            if card_only:
                client = _BudgetedClient(client, self.config.card_timeout_seconds)
                harness = ToolCallHarness(client, self.tools, config=replace(
                    self.harness.config, finalize_on_budget=True,
                    reserve_final_turn=True, report_budget=True,
                    allow_one_read_budget_correction=True,
                    finalization_instruction=(
                        "Reviewer 单卡预算收尾。不得再调用工具；仅使用已返回的 Reader 片段和原 Evidence。"
                        "按单卡 ReviewerCardDraft schema 输出严格 JSON；真实 read_id 可放入 read_citations，"
                        "不得生成 review_evidence、证据 ID、原文摘录或运行原因。"
                        "保留已支持的有限特征对应，未核验的关键项应为 unknown 或证据不足。"
                    ),
                ))
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
            try:
                draft = ReviewerCardDraft.model_validate_json(_extract_json(result.final_content))
            except (ValidationError, ValueError) as parse_error:
                _record_review_event("model_output_contract_violation", request, {
                    "error": str(parse_error), "raw_model_output": result.final_content,
                    "draft_schema": ReviewerCardDraft.model_json_schema(),
                })
                repaired = await repair_json(client, result.final_content,
                                             ReviewerCardDraft.model_json_schema(), call_options)
                draft = ReviewerCardDraft.model_validate_json(_extract_json(repaired))
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
            review = _register_review_reads(review, request, result.trace, catalog)
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

    async def review_card(self, request: NoveltyPointReviewRequest) -> ReviewerSupportReview | NoveltyPointReview:
        """Assess one Card's support; retain an explicit legacy compatibility path."""
        if len(request.cards) != 1:
            raise ValueError("review_card requires exactly one card")
        if self.config.controlled_reading:
            from .reviewer_card import review_controlled_card
            return await review_controlled_card(self, request.model_copy(deep=True))
        try:
            return await asyncio.wait_for(
                self._review_point(request, card_only=True), self.config.card_timeout_seconds
            )
        except TimeoutError as exc:
            return _insufficient_review(request.novelty_point.point_id, "单卡评审超过时间预算，核验未完成。", cause="budget_exhausted").model_copy(
                update={"execution_issues": [_review_exception_issue(exc, request, "model")]})

    def _summary_checkpoint_prompt(self) -> str:
        return self._render_instruction("reviewer/summarize_reviews", _SUMMARY_FALLBACK) + _SUMMARY_CONTRACT

    async def summarize_with_checkpoint(self, request, card_reviews, *, output_root, run_id):
        """Persist verified Card results before the existing summary call."""
        from .reviewer_checkpoint import ReviewerSummaryCheckpoint, ReviewSummaryAttempt
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
        return await checkpoint.attempt(self)

    async def recover_summary(self, request, *, output_root, run_id, checkpoint_id):
        """Explicit one-attempt summary recovery, with no retrieval or Reader tool call."""
        from .reviewer_checkpoint import ReviewerSummaryCheckpoint
        checkpoint = ReviewerSummaryCheckpoint.load(self, request, output_root=output_root,
            run_id=run_id, checkpoint_id=checkpoint_id)
        return await checkpoint.attempt(self, recovery=True)

    async def summarize_reviews(self, request, card_reviews, *, _input_date=None) -> NoveltyPointReview:
        """Synthesize compact card reviews; validate against originals locally."""
        deadline = time.monotonic() + self.config.summary_timeout_seconds
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
            user = json.dumps({
                "today": _input_date or self.config.summary_input_date or datetime.now(timezone.utc).date().isoformat(),
                "novelty_point": request.novelty_point.model_dump(mode="json"),
                "card_reviews": projected,
                "review_schema": ReviewerSummaryDraft.model_json_schema(),
                "recheck_schema": SummaryRecheckRequest.model_json_schema(),
                "feature_catalog": _feature_catalog(request.novelty_point),
            }, ensure_ascii=False)
            lineage = _verify_summary_payload(rows, user)
            _record_review_event("summary_payload_prepared", request, {
                "sent_to_model": False, "payload_sha256": hashlib.sha256(user.encode()).hexdigest(),
                "evidence_lineage": lineage})
            remaining = _summary_remaining(deadline)
            transport_timeout = max(0.001, remaining - min(1.0, remaining * 0.1))
            summary_options = replace(self.model_options or ModelCallOptions(), tools=(), tool_choice="none",
                                      response_format=None, timeout_seconds=transport_timeout)
            _record_review_event("summary_client_entered", request, {
                "payload_sha256": hashlib.sha256(user.encode()).hexdigest(),
                "actual_model_input": user, "evidence_lineage": lineage,
                "remaining_seconds": remaining, "transport_timeout_seconds": transport_timeout})
            verification = SummaryVerification(request, rows,
                max_chars=self.config.summary_max_recheck_chars, max_requests=self.config.summary_max_rechecks)
            messages = [ChatMessage(role="system", content=with_chinese_explanations(system)),
                        ChatMessage(role="user", content=user)]
            model_calls = 0
            while True:
                phase = "model"
                if model_calls >= self.config.summary_max_calls:
                    raise ToolCallBudgetExhausted("summary model call budget exhausted")
                model_calls += 1
                response = await asyncio.wait_for(self._client().acomplete(messages,
                    options=replace(summary_options, timeout_seconds=_summary_remaining(deadline))),
                    _summary_remaining(deadline))
                _record_review_event("summary_response_received", request, {
                    "payload_sha256": hashlib.sha256(user.encode()).hexdigest(),
                    "raw_model_output": response.content})
                if response.tool_calls:
                    raise ValueError("summary attempted a tool call")
                phase = "schema"

                def parse_step(content):
                    payload = json.loads(_extract_json(content))
                    model = SummaryRecheckRequest if isinstance(payload, dict) and "recheck" in payload else ReviewerSummaryDraft
                    return model.model_validate(payload)

                try:
                    step = parse_step(response.content)
                except (ValidationError, ValueError) as parse_error:
                    _record_review_event("model_output_contract_violation", request, {
                        "stage": "summary", "error": str(parse_error), "raw_model_output": response.content,
                        "draft_schema": ReviewerSummaryDraft.model_json_schema()})
                    remaining = _summary_remaining(deadline)
                    if model_calls >= self.config.summary_max_calls:
                        raise ToolCallBudgetExhausted("summary model repair budget exhausted")
                    model_calls += 1
                    # Repair the chosen response kind; malformed final drafts retain the historical contract.
                    repair_schema = (SummaryRecheckRequest if '"recheck"' in (response.content or "")
                                     else ReviewerSummaryDraft).model_json_schema()
                    repaired = await asyncio.wait_for(repair_json(self._client(), response.content,
                        repair_schema, replace(summary_options, timeout_seconds=remaining),
                        validation_error=parse_error), remaining)
                    step = parse_step(repaired)
                if isinstance(step, ReviewerSummaryDraft):
                    draft = step
                    break
                phase = "reference"
                record = verification.recheck(step.recheck)
                _record_review_event("summary_targeted_verification", request, record)
                messages = [*messages, ChatMessage(role="assistant", content=step.model_dump_json()),
                    ChatMessage(role="user", content=json.dumps({"targeted_verification": record,
                        "instruction": "仅核对此特征与这些依据；返回最终草稿或另一个明确的复核请求。"}, ensure_ascii=False))]
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
                raise ValueError("summary cites evidence not selected in completed card handoff")
            review = _validate_review_references(review, request)
            comparison_origins = verification.validate(review)
            review = _validate_verdict_coverage(review, request)
            _summary_remaining(deadline)
            _record_review_event("summary_result", request, {
                "rows": rows, "actual_model_input": user,
                "draft_schema": ReviewerSummaryDraft.model_json_schema(),
                "raw_draft": draft.model_dump(mode="json"),
                "raw_model_output": response.content, "review": review.model_dump(mode="json"),
                "legacy_guard_shadow": _legacy_guard_shadow(review, request),
                "comparison_origins": comparison_origins,
                "targeted_verifications": verification.records,
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
                "response_received": "response" in locals(),
                "model_draft_valid": False})
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
                [item.model_dump(mode="json") for item in request.evidence],
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




def _register_review_reads(review, request, trace, catalog):
    reads = {}
    for event in trace:
        observation = getattr(event, "observation", None)
        if getattr(event, "kind", None) != "tool_result" or observation is None \
                or observation.tool_name != "reader" or not observation.succeeded:
            continue
        payload = observation.payload
        for raw in ([payload["read_result"]] if "read_result" in payload else payload.get("read_results", [])):
            if raw.get("char_end", 0) > raw.get("char_start", 0):
                reads[raw["read_id"]] = raw
    requested = {cite.read_id: cite for cite in review.read_citations}
    requested.update({ref: None for work in review.highly_relevant_works
                      for ref in work.evidence_ids if ref.startswith("read_") and ref not in requested})
    requested.update({ref: None for comparison in review.feature_comparisons
                      for ref in comparison.evidence_refs if ref.startswith("read_") and ref not in requested})
    bound_work = {item.work_id for item in request.evidence
                  if any(item.evidence_id in card.evidence_ids for card in request.cards)}
    registered, id_map = [], {}
    for read_id, cite in requested.items():
        raw = reads.get(read_id)
        if raw is None:
            raise ValueError(f"unread or empty read_id {read_id}")
        entry = next((item for item in catalog if item["artifact_id"] == raw["artifact_id"]
                      and item["namespace"] == raw["namespace"]
                      and item["work_id"] == raw["work_id"]), None)
        if entry is None or raw["work_id"] not in bound_work \
                or (entry["content_hash"] is not None and entry["content_hash"] != raw["sha256"]):
            raise ValueError("read citation is outside the authorized material catalog")
        start = raw["char_start"] if cite is None or cite.char_start is None else cite.char_start
        end = raw["char_end"] if cite is None or cite.char_end is None else cite.char_end
        if not raw["char_start"] <= start < end <= raw["char_end"]:
            raise ValueError("read citation range is outside the returned text")
        quote = raw["text"][start - raw["char_start"]:end - raw["char_start"]]
        cards = [card for card in request.cards if any(
            item.work_id == raw["work_id"] and item.evidence_id in card.evidence_ids
            for item in request.evidence)]
        if len(cards) != 1:
            raise ValueError("read citation must bind one Card and Work")
        card = cards[0]
        digest = hashlib.sha256(f"{card.card_id}\x1f{read_id}\x1f{start}\x1f{end}".encode()).hexdigest()
        evidence_id = "rev_ev_" + digest[:24]
        id_map[read_id] = evidence_id
        registered.append(ReviewEvidence(
            evidence_id=evidence_id, review_id="review_" + hashlib.sha256(card.card_id.encode()).hexdigest()[:20],
            origin_card_id=card.card_id, novelty_point_id=request.novelty_point.point_id,
            work_id=raw["work_id"], source_record_id=entry["source_record_id"],
            artifact_id=raw["artifact_id"], namespace=raw["namespace"],
            artifact_hash=raw["sha256"], read_id=read_id, char_start=start, char_end=end,
            exact_quote=quote, role=entry["role"], content_extent=entry["content_extent"],
            version_label=entry["version_label"],
        ))
    works = [work.model_copy(update={"evidence_ids": [id_map.get(eid, eid) for eid in work.evidence_ids]})
             for work in review.highly_relevant_works]
    comparisons = [item.model_copy(update={"evidence_refs": [id_map.get(ref, ref) for ref in item.evidence_refs]})
                   for item in review.feature_comparisons]
    return review.model_copy(update={"highly_relevant_works": works,
                             "feature_comparisons": comparisons, "review_evidence": registered})


def _compact_summary_rows(request, card_reviews):
    """Validate persisted results and build source-free, explicitly selected handoffs."""
    cards = {card.card_id: card for card in request.cards}
    evidence = {item.evidence_id: item for item in request.evidence}
    rows, validated_ids = [], set()
    card_ids = [row.get("card_id") for row in card_reviews]
    if len(set(card_ids)) != len(card_ids):
        raise ValueError("duplicate card judgment in summary handoff")
    for row in card_reviews:
        card = cards.get(row.get("card_id"))
        if card is None or row.get("novelty_point_id") != request.novelty_point.point_id:
            raise ValueError("indexed card review is outside request scope")
        compact = {key: row[key] for key in ("index", "card_id", "novelty_point_id", "status")}
        compact["document_title"] = card.document_title
        if row.get("status") != "completed":
            compact["error"] = str(row.get("error", "单卡核验未完成"))[:500]
            compact["handoff"] = CardSummaryHandoff(**compact).model_dump(mode="json")
            rows.append(compact)
            continue
        review = parse_card_review(row.get("review"))
        single = NoveltyPointReviewRequest(
            subject_paper_id=request.subject_paper_id,
            novelty_point=request.novelty_point, tasks=request.tasks, cards=[card],
            evidence=[item for item in request.evidence if item.evidence_id in card.evidence_ids],
        )
        _validate_review_references(review, single)
        compact["review"] = review.model_dump(mode="json")
        # Full records remain available for provenance/recovery, never as default model input.
        compact["review_evidence"] = [item.model_dump(mode="json") for item in review.review_evidence]
        sources = {**evidence, **{item.evidence_id: item for item in review.review_evidence}}
        handoff = card_handoff(compact, review, sources)
        compact["handoff"] = handoff.model_dump(mode="json")
        validated_ids.update(item.evidence_id for item in handoff.selected_evidence)
        rows.append(compact)
    return rows, validated_ids


def _summary_model_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Whitelist the explicit handoff contract instead of filtering persisted records."""
    return [CardSummaryHandoff.model_validate(row["handoff"]).model_dump(mode="json") for row in rows]


def _verify_summary_payload(rows: list[dict[str, Any]], user: str) -> list[dict[str, Any]]:
    """Verify the exact serialized handoff; no text expansion or citation loss allowed."""
    actual = json.loads(user)["card_reviews"]
    if actual != _summary_model_rows(rows):
        raise ValueError("summary payload changed card judgment handoff or selected evidence")
    return [dict(handle) for row in actual for handle in row["selected_evidence"]]


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
