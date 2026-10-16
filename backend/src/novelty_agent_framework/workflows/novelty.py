"""基于 LangGraph 的论文查新总分总工作流。"""

from __future__ import annotations

import asyncio
import contextvars
import inspect
import json
import re
import uuid
from collections.abc import Awaitable
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable, Mapping, TypeVar, cast

from langgraph.graph import END, START, StateGraph
from langgraph.types import Send
from pydantic import ValidationError

from novelty_agent_framework.core.errors import WorkflowExecutionError
from novelty_agent_framework.core.integrity_gates import (
    validate_report_integrity,
    validate_review_integrity,
)
from novelty_agent_framework.core.report_binding import (
    bind_reviews_to_report,
    bind_point_lifecycle_to_report,
    bind_search_coverage_to_report,
)
from novelty_agent_framework.core.target_paper_identity import identity_from_paper
from novelty_agent_framework.core.runtime_artifacts import (
    RuntimeDebugConfig,
    RuntimeArtifactManager,
    current_runtime_artifacts,
)
from novelty_agent_framework.persistence import (
    ReferenceStore,
    persist_evidence_cards,
    persist_novelty_points,
    persist_novelty_reviews,
    persist_report,
    persist_retrieval_plans,
    persist_task_research_result,
    persist_task_retrieval_audit,
    persist_workflow_input,
)
from novelty_agent_framework.tools.renderer import render_report

from ..schemas.domain import PartialCardReview

from ..agents import (
    DefaultEvidenceValidator,
    DemoCoordinator,
    DemoPointExtractor,
    DemoSearchPlanner,
    DemoTaskResearcher,
    build_paper_digest,
)
from ..schemas import (
    EvidenceCard,
    InsufficientFinalEvidence,
    IssueSeverity,
    NoveltyBrief,
    NoveltyPoint,
    NoveltyPointReview,
    NoveltyPointReviewRequest,
    ReviewStatus,
    SupplementRequest,
    NoveltyReport,
    NoveltyRunResult,
    PaperInput,
    RejectedEvidence,
    ResearchTask,
    SearchPlan,
    TaskResearchRequest,
    TaskResearchResult,
    TaskResearchStatus,
    WorkflowIssue,
)
from .state import NoveltyState, NoveltyWorkflowConfig, NoveltyWorkflowServices
from ..core.recovery_policy import plan_recovery
from ..schemas.failures import RecoveryAction, FailureCode, FailureScope, make_failure

T = TypeVar("T")
ProgressCallback = Callable[[str, int | None], None | Awaitable[None]]

_progress_callback: contextvars.ContextVar[ProgressCallback | None] = (
    contextvars.ContextVar("novelty_progress_callback", default=None)
)

_STAGE_NAMES = (
    "extract_points",
    "plan",
    "dispatch_planning_tasks",
    "plan_research_task",
    "dispatch_research_tasks",
    "run_research_task",
    "validate_evidence",
    "review_evidence",
    "validate_synthesis_input",
    "check_final_evidence_sufficiency",
    "plan_supplement",
    "synthesize_report",
    "validate_report_integrity",
    "persist_report",
    "render_report",
)


async def _resolve(value: T | Awaitable[T]) -> T:
    """兼容同步实现和异步实现，便于接入不同模型 SDK。"""

    if inspect.isawaitable(value):
        return await cast(Awaitable[T], value)
    return value


def _safe_error(exc: Exception) -> str:
    message = re.sub(
        r"(?i)(api[_-]?key|authorization|cookie)\s*[:=]\s*\S+",
        r"\1=<redacted>",
        str(exc),
    )
    message = re.sub(r"(?:/[\w. -]+){2,}", "<path>", message)
    return f"{type(exc).__name__}: {message}"[:1000]


def _insufficient_point_review(
    point_id: str, reason: str
) -> NoveltyPointReview:
    """Workflow execution fallback; no semantic evidence judgment took place."""
    return NoveltyPointReview(
        novelty_point_id=point_id,
        status=ReviewStatus.INSUFFICIENT_EVIDENCE,
        supplement_request=SupplementRequest(reason=reason),
        incomplete_reason="technical_error",
    )


class NoveltyWorkflow:
    """把规划、并行调研、证据门控和汇总组织成可运行闭环。"""

    @classmethod
    def default(
        cls, *, output_root: str | Path | None = None
    ) -> "NoveltyWorkflow":
        """构造默认查新工作流。

        当前项目采用固定 Agent 组合，因此默认装配逻辑直接放在工作流类中。
        """

        config = None
        if output_root is not None:
            config = NoveltyWorkflowConfig(
                runtime_debug=RuntimeDebugConfig(
                    output_root=Path(output_root),
                    archive_root=Path(output_root) / "runtime-archive",
                )
            )
        return cls(
            NoveltyWorkflowServices(
                coordinator=DemoCoordinator(),
                task_researcher=DemoTaskResearcher(),
                search_planner=DemoSearchPlanner(),
                point_extractor=DemoPointExtractor(),
            ),
            config=config,
            output_root=output_root,
        )

    def __init__(
        self,
        services: NoveltyWorkflowServices,
        config: NoveltyWorkflowConfig | None = None,
        *,
        runtime_config: Mapping[str, Any] | None = None,
        output_root: str | Path | None = None,
    ) -> None:
        self.services = services
        self.config = config or NoveltyWorkflowConfig()
        self.output_root = Path(
            output_root
            if output_root is not None
            else self.config.runtime_debug.output_root
        )
        if Path(self.config.runtime_debug.output_root) != self.output_root:
            self.config = replace(
                self.config,
                runtime_debug=replace(
                    self.config.runtime_debug, output_root=self.output_root
                ),
            )
        self.validator = services.validator or DefaultEvidenceValidator()
        self.point_extractor = services.point_extractor or DemoPointExtractor()
        evidence_builder = getattr(services.task_researcher, "evidence_builder", None)
        self.reference_store = (
            getattr(evidence_builder, "reference_store", None)
            or getattr(services.task_researcher, "reference_store", None)
            or ReferenceStore(self.output_root)
        )
        self.runtime_config = dict(runtime_config or {})
        self.last_runtime_run_id: str | None = None
        self.last_rendered_report_path: str | None = None
        self.graph = self._build_graph()

    def _build_graph(self) -> Any:
        builder = StateGraph(NoveltyState)
        builder.add_node(
            "extract_points", self._record_stage("extract_points", self._extract_points)
        )
        builder.add_node("plan", self._record_stage("plan", self._plan))
        builder.add_node(
            "dispatch_planning_tasks",
            self._record_stage("dispatch_planning_tasks", self._dispatch_node),
        )
        builder.add_node(
            "plan_research_task",
            self._record_stage("plan_research_task", self._plan_research_task),
        )
        builder.add_node(
            "dispatch_research_tasks",
            self._record_stage("dispatch_research_tasks", self._dispatch_node),
        )
        builder.add_node(
            "run_research_task",
            self._record_stage("run_research_task", self._run_research_task),
        )
        builder.add_node(
            "validate_evidence",
            self._record_stage("validate_evidence", self._validate_evidence),
        )
        builder.add_node(
            "review_evidence",
            self._record_stage("review_evidence", self._review_evidence),
        )
        builder.add_node(
            "validate_synthesis_input",
            self._record_stage(
                "validate_synthesis_input", self._validate_synthesis_input
            ),
        )
        builder.add_node(
            "check_final_evidence_sufficiency",
            self._record_stage(
                "check_final_evidence_sufficiency",
                self._check_final_evidence_sufficiency,
            ),
        )
        builder.add_node(
            "plan_supplement",
            self._record_stage("plan_supplement", self._plan_supplement),
        )
        builder.add_node(
            "synthesize_report",
            self._record_stage("synthesize_report", self._synthesize_report),
        )
        builder.add_node(
            "validate_report_integrity",
            self._record_stage(
                "validate_report_integrity", self._validate_report_integrity
            ),
        )
        builder.add_node(
            "persist_report",
            self._record_stage("persist_report", self._persist_report),
        )
        builder.add_node(
            "render_report",
            self._record_stage("render_report", self._render_report),
        )

        builder.add_edge(START, "extract_points")
        builder.add_edge("extract_points", "plan")
        builder.add_edge("plan", "dispatch_planning_tasks")
        builder.add_conditional_edges(
            "dispatch_planning_tasks",
            self._dispatch_planning_tasks,
            ["plan_research_task", "dispatch_research_tasks"],
        )
        builder.add_edge("plan_research_task", "dispatch_research_tasks")
        builder.add_conditional_edges(
            "dispatch_research_tasks",
            self._dispatch_research_tasks,
            ["run_research_task", "validate_evidence"],
        )
        builder.add_edge("run_research_task", "validate_evidence")
        builder.add_edge("validate_evidence", "review_evidence")
        builder.add_edge("review_evidence", "validate_synthesis_input")
        builder.add_edge(
            "validate_synthesis_input", "check_final_evidence_sufficiency"
        )
        builder.add_conditional_edges(
            "check_final_evidence_sufficiency",
            self._route_after_evidence_sufficiency_check,
            {
                "supplement": "plan_supplement",
                "synthesize": "synthesize_report",
            },
        )
        builder.add_edge("plan_supplement", "dispatch_planning_tasks")
        builder.add_edge("synthesize_report", "validate_report_integrity")
        builder.add_edge("validate_report_integrity", "persist_report")
        builder.add_edge("persist_report", "render_report")
        builder.add_edge("render_report", END)
        return builder.compile()

    @staticmethod
    def _record_stage(
        stage_name: str,
        function: Callable[[NoveltyState], Awaitable[dict[str, Any]]],
    ) -> Callable[[NoveltyState], Awaitable[dict[str, Any]]]:
        """Instrument a graph node without giving business agents artifact duties."""

        async def recorded(state: NoveltyState) -> dict[str, Any]:
            callback = _progress_callback.get()
            if callback is not None:
                await _resolve(callback(stage_name, state.get("rounds") or None))
            runtime = current_runtime_artifacts()
            if runtime is None:
                return await function(state)
            handle = runtime.start_stage(stage_name, state)
            try:
                output = await function(state)
            except BaseException as exc:
                runtime.fail_stage(handle, exc)
                raise
            runtime.finish_stage(handle, output)
            return output

        recorded.__name__ = f"runtime_recorded_{stage_name}"
        return recorded

    async def _extract_points(self, state: NoveltyState) -> dict[str, Any]:
        persist_workflow_input(state["paper"], output_root=self.output_root)
        try:
            digest = build_paper_digest(state["paper"])
            try:
                points_value = self.point_extractor.extract(
                    digest,
                    previous_brief=None,
                    attempt=1,
                )
            finally:
                runtime = current_runtime_artifacts()
                trace = getattr(self.point_extractor, "last_trace", None)
                if runtime is not None and trace:
                    runtime.record_diagnostic_artifact("point_extraction_trace.json", trace)
            points = list(await _resolve(points_value))
            validated = [
                point
                if isinstance(point, NoveltyPoint)
                else NoveltyPoint.model_validate(point)
                for point in points
            ]
        except (ValidationError, TypeError, ValueError) as exc:
            raise WorkflowExecutionError(f"查新点提取失败：{exc}") from exc
        if not validated:
            raise WorkflowExecutionError("查新点提取结果为空")
        # 测试版持久化：写入该论文独立工作目录（后续可替换为数据库存储）
        persist_novelty_points(
            state["paper"], validated, output_root=self.output_root
        )
        missing_english = [
            point.point_id
            for point in validated
            if not point.claim_en or not point.technical_features_en
        ]
        issues: list[WorkflowIssue] = []
        trace = getattr(self.point_extractor, "last_trace", None)
        if trace and trace.get("scope_status") in {"pending_dedup", "pending_coverage"}:
            issues.append(WorkflowIssue(
                node="extract_points", code=("point_deduplication_unresolved" if trace.get("scope_status") == "pending_dedup" else "point_coverage_unresolved"),
                message=("查新候选已保留供逐点研究；保留不代表其独立贡献已确认。"
                         "特征保留账本不代表作者贡献已获语义确认，提取范围仍待核查。"),
                severity=IssueSeverity.WARNING,
            ))
        if trace and trace.get("exit_reason") == "below_target_review_scope":
            issues.append(WorkflowIssue(
                node="extract_points", code="point_scope_below_target",
                message=(f"提取器只产生 {len(validated)} 个查新候选；"
                         "目标数量未达到，后续检索范围可能不完整。"),
                severity=IssueSeverity.WARNING,
            ))
        if missing_english:
            issues.append(
                WorkflowIssue(
                    node="extract_points",
                    code="missing_english_point",
                    message=(
                        "以下查新点缺少完整英文表述；英文检索任务将由 SearchPlanner "
                        "基于中文内容生成英文检索表达："
                        + ", ".join(missing_english)
                    ),
                    severity=IssueSeverity.WARNING,
                )
            )
        return {"novelty_points": validated, "issues": issues,
                "point_coverage": (trace or {}).get("coverage_ledger", {})}

    async def _plan(self, state: NoveltyState) -> dict[str, Any]:
        try:
            brief_value = self.services.coordinator.plan(
                state["paper"],
                points=state.get("novelty_points", []),
                attempt=1,
            )
            brief = NoveltyBrief.model_validate(await _resolve(brief_value))
        except (ValidationError, TypeError, ValueError) as exc:
            raise WorkflowExecutionError(f"Coordinator 未生成合法 NoveltyBrief：{exc}") from exc

        return {
            "brief": brief,
            "research_tasks": list(brief.research_tasks),
            "all_research_tasks": list(brief.research_tasks),
            "rounds": 1,
        }

    async def _dispatch_node(self, state: NoveltyState) -> dict[str, Any]:
        """显式 fan-out 汇合点；不把全局状态传入任务 Researcher。"""

        return {}

    async def _dispatch_planning_tasks(self, state: NoveltyState):
        tasks = state.get("research_tasks", [])
        if not tasks:
            return "dispatch_research_tasks"
        points = {item.point_id: item for item in state.get("novelty_points", [])}
        sends = [
            Send(
                "plan_research_task",
                {
                    "current_point": points[task.novelty_point_id],
                    "current_task": task,
                    "subject_paper_id": state["paper"].paper_id,
                    "recovery_search_plans": state.get("recovery_search_plans", {}),
                },
            )
            for task in tasks
            if task.novelty_point_id in points
        ]
        return sends or "dispatch_research_tasks"

    async def _plan_research_task(self, state: NoveltyState) -> dict[str, Any]:
        point = state["current_point"]
        task = state["current_task"]
        prior_plan = state.get("recovery_search_plans", {}).get(_recovery_key(point.point_id, task.task_id))
        if prior_plan is not None:
            return {"search_plans": [prior_plan]}
        try:
            planner = self.services.search_planner
            async_plan = getattr(planner, "aplan", None)
            if callable(async_plan):
                plan_value = await async_plan(point, task)
            else:
                # Legacy synchronous planners must not block graph fan-out.
                plan_value = await asyncio.to_thread(planner.plan, point, task)
            plan = SearchPlan.model_validate(await _resolve(plan_value))
        except (ValidationError, TypeError, ValueError) as exc:
            # 韧性：单个任务检索方案失败不应击穿整个查新流程——
            # 记录 PARTIAL 结果与告警，其余任务继续，最终数量检查可见该缺口。
            safe_error = _safe_error(exc)
            result = TaskResearchResult(
                task_id=task.task_id,
                novelty_point_id=point.point_id,
                status=TaskResearchStatus.FAILED,
                warnings=[f"planner failed: {safe_error}"],
                steps_used=0,
            )
            persist_task_research_result(
                state["subject_paper_id"],
                result,
                attempt=task.attempt,
                output_root=self.output_root,
            )
            return {
                "search_plans": [],
                "task_research_results": [result],
                "raw_evidence": [],
                "raw_evidence_cards": [],
                "issues": [
                    WorkflowIssue(
                        node="plan_research_task",
                        code="search_plan_failed",
                        message=(
                            f"检索方案生成失败（{point.point_id}/{task.task_id}）："
                            f"{safe_error}"
                        ),
                        task_id=task.task_id,
                    )
                ],
            }
        if plan.task_id != task.task_id or plan.novelty_point_id != point.point_id:
            raise WorkflowExecutionError(
                f"SearchPlan 与任务绑定不一致：{point.point_id} / {task.task_id}"
            )
        return {"search_plans": [plan]}

    async def _dispatch_research_tasks(self, state: NoveltyState):
        tasks = state.get("research_tasks", [])
        if not tasks:
            return "validate_evidence"
        _require_complete_search_plans(
            state.get("all_research_tasks", tasks), state.get("search_plans", [])
        )
        points = {item.point_id: item for item in state.get("novelty_points", [])}
        plans = {
            (item.novelty_point_id, item.task_id): item
            for item in state.get("search_plans", [])
        }
        persist_retrieval_plans(
            state["paper"],
            state.get("all_research_tasks", []),
            search_plans=state.get("search_plans", []),
            rounds=state.get("rounds", 0),
            point_order=[item.point_id for item in state.get("novelty_points", [])],
            output_root=self.output_root,
        )
        sends = []
        for task in tasks:
            point = points.get(task.novelty_point_id)
            plan = plans.get((task.novelty_point_id, task.task_id))
            if point is None or plan is None:
                continue
            sends.append(
                Send(
                    "run_research_task",
                    {
                        "subject_paper_id": state["paper"].paper_id,
                        "run_id": state["run_id"],
                        "current_point": point,
                        "current_task": task,
                        "current_search_plan": plan,
                        "target_identity": identity_from_paper(state["paper"]),
                        "current_recovery": state.get("recovery_directives", {}).get(_recovery_key(point.point_id, task.task_id)),
                    },
                )
            )
        return sends or "validate_evidence"

    async def _run_research_task(self, state: NoveltyState) -> dict[str, Any]:
        task = state["current_task"]
        point = state["current_point"]
        request = TaskResearchRequest(
            subject_paper_id=state["subject_paper_id"],
            run_id=state["run_id"],
            novelty_point=point,
            research_task=task,
            search_plan=state["current_search_plan"],
            target_identity=state.get("target_identity"),
            recovery=state.get("current_recovery"),
        )
        try:
            result = TaskResearchResult.model_validate(
                await self.services.task_researcher.ainvoke(request)
            )
            issues: list[WorkflowIssue] = []
        except Exception as exc:
            safe_error = _safe_error(exc)
            result = TaskResearchResult(
                task_id=task.task_id,
                novelty_point_id=point.point_id,
                status=TaskResearchStatus.FAILED,
                warnings=[f"task researcher failed: {safe_error}"],
                execution_failures=[make_failure(FailureCode.UNKNOWN, scope=FailureScope(
                    paper_id=request.subject_paper_id, run_id=request.run_id, point_id=point.point_id,
                    task_id=task.task_id), message="Research task raised an unclassified execution error.")],
                steps_used=0,
            )
            issues = [
                WorkflowIssue(
                    node="run_research_task",
                    code="research_task_failed",
                    message=(
                        f"调研任务 {point.point_id} / {task.task_id} 执行失败："
                        f"{safe_error}"
                    ),
                    task_id=task.task_id,
                )
            ]
        persist_task_research_result(
            state["subject_paper_id"],
            result,
            attempt=task.attempt,
            output_root=self.output_root,
        )
        return {
            "task_research_results": [result],
            "raw_evidence": result.evidence,
            "raw_evidence_cards": result.evidence_cards,
            "issues": issues,
        }

    async def _validate_evidence(self, state: NoveltyState) -> dict[str, Any]:
        persist_task_retrieval_audit(
            state["paper"],
            state.get("all_research_tasks", []),
            state.get("task_research_results", []),
            search_plans=state.get("search_plans", []),
            rounds=state.get("rounds", 0),
            point_order=[item.point_id for item in state.get("novelty_points", [])],
            output_root=self.output_root,
        )
        result_value = self.validator.validate(
            state.get("raw_evidence_cards", []),
            tasks=state.get("all_research_tasks", []),
            evidence=state.get("raw_evidence", []),
            novelty_points=state.get("novelty_points", []),
            paper_id=state["paper"].paper_id,
            reference_store=self.reference_store,
        )
        result = await _resolve(result_value)

        rejected = [
            RejectedEvidence(card_id=card_id, reason=reason)
            for card_id, reason in result.rejected
        ]
        issues = [
            WorkflowIssue(
                node="validate_evidence",
                code=code,
                message=message,
                severity=IssueSeverity(severity),
                task_id=task_id,
            )
            for code, message, severity, task_id in result.issues
        ]
        persist_evidence_cards(
            state["paper"], raw_cards=state.get("raw_evidence_cards", []),
            validator_accepted_cards=result.accepted, accepted_cards=result.accepted,
            rejected_evidence=rejected, output_root=self.output_root,
        )
        return {
            "evidence_integrity": result.integrity,
            "integrity_rejected_card_ids": result.integrity.get("provenance_rejected_card_ids", []),
            "validator_accepted_cards": list(result.accepted),
            "evidence_cards": list(result.accepted),
            "rejected_evidence": rejected,
            "issues": issues,
        }

    async def _review_evidence(self, state: NoveltyState) -> dict[str, Any]:
        """按 NoveltyPoint 综合证据；Reviewer 不过滤 Validator 放行的卡片。"""

        validator_accepted = state.get("validator_accepted_cards", state.get("evidence_cards", []))
        accepted = list(validator_accepted)
        rejected = list(state.get("rejected_evidence", []))
        reviews: list[NoveltyPointReview] = []
        decisions = []
        issues: list[WorkflowIssue] = []
        audit_decisions = None
        card_reviews = None
        summary_attempts = []

        if self.services.reviewer is None:
            reviews = [
                _insufficient_point_review(
                    point.point_id, "Reviewer 未启用，未执行查新点级信息判定。"
                )
                for point in state.get("novelty_points", [])
            ]
            issues = [
                WorkflowIssue(
                    node="review_evidence",
                    code="reviewer_unavailable",
                    message="Reviewer 未提供或未运行；该结果不是证据不足裁定。",
                    severity=IssueSeverity.ERROR,
                )
            ]
        else:
            try:
                # Detect the legacy signature before scheduling any point reviews.
                # A TypeError inside a modern reviewer must not restart a batch.
                inspect.signature(self.services.reviewer.review).bind(None)
                evidence_by_id = {
                    item.evidence_id: item for item in state.get("raw_evidence", [])
                }
                semaphore = asyncio.Semaphore(self.config.max_concurrency)
                two_stage = callable(getattr(self.services.reviewer, "review_card", None)) and callable(
                    getattr(self.services.reviewer, "summarize_reviews", None)
                )
                if two_stage:
                    card_reviews = [
                        {"index": index, "card_id": card.card_id,
                         "novelty_point_id": card.novelty_point_id,
                         "status": "pending", "review": None}
                        for index, card in enumerate(validator_accepted, 1)
                    ]
                def checkpoint():
                    # Synchronous single-event-loop write; workers never replace
                    # the file with their own partial view.
                    persist_novelty_reviews(
                        state["paper"], [], output_root=self.output_root,
                        card_reviews=card_reviews, phase="card_review",
                    )
                if two_stage:
                    checkpoint()

                async def review_point(point):
                    try:
                        return await invoke_point(point), None
                    except Exception as exc:
                        safe_error = _safe_error(exc)
                        return _insufficient_point_review(
                            point.point_id, f"Reviewer 执行失败：{safe_error}"
                        ), WorkflowIssue(
                            node="review_evidence", code="review_failed",
                            message=f"Reviewer {point.point_id} 执行失败：{safe_error}",
                            severity=IssueSeverity.ERROR,
                        )

                async def review_card(request, card):
                    row = next(r for r in card_reviews if r["card_id"] == card.card_id
                               and r["novelty_point_id"] == card.novelty_point_id)
                    async with semaphore:
                        row["status"] = "running"
                        checkpoint()
                        try:
                            single = NoveltyPointReviewRequest(
                                subject_paper_id=request.subject_paper_id,
                                novelty_point=request.novelty_point, tasks=request.tasks,
                                cards=[card], evidence=[e for e in request.evidence
                                                       if e.evidence_id in card.evidence_ids],
                            )
                            result = NoveltyPointReview.model_validate(await _resolve(
                                self.services.reviewer.review_card(single)
                            ))
                            if result.novelty_point_id != card.novelty_point_id:
                                raise ValueError("card review returned a different point")
                            row.update(status="completed", review=result.model_dump(mode="json"))
                        except Exception as exc:
                            row.update(status="failed", error=_safe_error(exc))
                        checkpoint()
                    return row

                async def invoke_point(point):
                    point_cards = [
                        card for card in validator_accepted
                        if card.novelty_point_id == point.point_id
                    ]
                    requested_ids = {
                        evidence_id for card in point_cards
                        for evidence_id in card.evidence_ids
                    }
                    request = NoveltyPointReviewRequest(
                        subject_paper_id=state["paper"].paper_id,
                        novelty_point=point,
                        tasks=[
                            task for task in state.get("all_research_tasks", [])
                            if task.novelty_point_id == point.point_id
                        ],
                        cards=point_cards,
                        evidence=[
                            evidence_by_id[evidence_id]
                            for evidence_id in requested_ids
                            if evidence_id in evidence_by_id
                            and evidence_by_id[evidence_id].novelty_point_id
                            == point.point_id
                        ],
                    )
                    if two_stage and request.cards and request.evidence:
                        rows = await asyncio.gather(*(review_card(request, card) for card in request.cards))
                        if all(row["status"] == "failed" for row in rows):
                            return _insufficient_point_review(point.point_id, "全部单卡评审执行失败。")
                        async with semaphore:
                            save_summary = getattr(self.services.reviewer, "summarize_with_checkpoint", None)
                            if callable(save_summary):
                                attempt = await _resolve(save_summary(request, rows, output_root=self.output_root,
                                    run_id=state.get("run_id", "untracked")))
                                review = attempt.review
                                summary_attempts.append({"point_id":point.point_id,
                                    **attempt.model_dump(mode="json")})
                            else:
                                review = await _resolve(self.services.reviewer.summarize_reviews(request, rows))
                    else:
                        async with semaphore:
                            review = await _resolve(self.services.reviewer.review(request))
                    review = NoveltyPointReview.model_validate(review)
                    if review.novelty_point_id != point.point_id:
                        raise ValueError("Reviewer returned a different novelty_point_id")
                    return review

                # gather preserves point order; persistence happens once after all
                # reviews finish. Each point receives all its cards and evidence.
                outcomes = await asyncio.gather(*(
                    review_point(point) for point in state.get("novelty_points", [])
                ))
                for review, issue in outcomes:
                    reviews.append(review)
                    if issue is not None:
                        issues.append(issue)
            except TypeError:
                # 迁移桥：兼容仍实现旧 cards/points/tasks 接口的外部 Reviewer。
                try:
                    result = await _resolve(
                        self.services.reviewer.review(
                            validator_accepted,
                            points=state.get("novelty_points", []),
                            tasks=state.get("all_research_tasks", []),
                        )
                    )
                except Exception as exc:
                    safe_error = _safe_error(exc)
                    accepted = []
                    reviews = [
                        _insufficient_point_review(
                            point.point_id, f"Reviewer 执行失败：{safe_error}"
                        )
                        for point in state.get("novelty_points", [])
                    ]
                    rejected.extend(
                        RejectedEvidence(card_id=card.card_id, reason="review_failed")
                        for card in validator_accepted
                    )
                    issues = [
                        WorkflowIssue(
                            node="review_evidence",
                            code="review_failed",
                            message=f"Reviewer 执行失败：{safe_error}",
                            severity=IssueSeverity.ERROR,
                        )
                    ]
                    result = None
                if result is None:
                    audit_decisions = decisions
                else:
                    accepted = list(result.accepted)
                    decisions = list(result.decisions)
                    rejected.extend(
                        RejectedEvidence(card_id=card_id, reason=reason)
                        for card_id, reason in result.rejected
                    )
                    issues = [
                        WorkflowIssue(
                            node="review_evidence",
                            code="needs_more_evidence",
                            message=f"证据卡 {card_id} 需要更多证据",
                        )
                        for card_id in result.needs_more
                    ]
                    issues.extend(
                        WorkflowIssue(
                            node="review_evidence",
                            code=decision.issues[0].code,
                            message=(
                                f"证据卡 {decision.card_id} 被审查拒绝："
                                f"{decision.issues[0].message}"
                            ),
                            severity=IssueSeverity(decision.issues[0].severity.value),
                        )
                        for decision in result.decisions
                        if decision.verdict.value == "reject" and decision.issues
                    )
                    audit_decisions = decisions
            except Exception as exc:
                safe_error = _safe_error(exc)
                reviewed_ids = {item.novelty_point_id for item in reviews}
                reviews.extend(
                    _insufficient_point_review(
                        point.point_id, f"Reviewer 执行失败：{safe_error}"
                    )
                    for point in state.get("novelty_points", [])
                    if point.point_id not in reviewed_ids
                )
                issues = [
                    WorkflowIssue(
                        node="review_evidence",
                        code="review_failed",
                        message=f"Reviewer 执行失败：{safe_error}",
                        severity=IssueSeverity.ERROR,
                    )
                ]

        persist_evidence_cards(
            state["paper"],
            raw_cards=state.get("raw_evidence_cards", []),
            validator_accepted_cards=(
                validator_accepted if self.services.reviewer else None
            ),
            review_decisions=audit_decisions,
            accepted_cards=accepted,
            rejected_evidence=rejected,
            output_root=self.output_root,
        )
        persist_novelty_reviews(
            state["paper"], reviews, output_root=self.output_root,
            card_reviews=card_reviews,
        )
        return {
            "evidence_cards": accepted,
            "rejected_evidence": rejected,
            "review_decisions": decisions,
            "novelty_reviews": reviews,
            "review_summary_attempts": summary_attempts,
            "review_card_results": card_reviews,
            "issues": issues,
        }

    async def _check_final_evidence_sufficiency(
        self, state: NoveltyState
    ) -> dict[str, Any]:
        """按查新点检查最终有效 EvidenceCard 数量是否达标。

        输入是 Validator 完成合法性和溯源核验后保留的 Card；Reviewer 只产出
        查新点级判断，不参与 Card 过滤。
        数量检查不评价语义质量；独立恢复策略结合技术执行事实和 Reviewer 缺口选择下一步。
        """
        brief = state["brief"]
        counts: dict[str, int] = {point.point_id: 0 for point in brief.novelty_points}
        for card in state.get("evidence_cards", []):
            if card.novelty_point_id in counts:
                counts[card.novelty_point_id] += 1

        configured_cut = self.config.min_final_evidence_cards_per_point
        insufficient = [
            InsufficientFinalEvidence(
                novelty_point_id=point.point_id,
                valid_card_count=counts[point.point_id],
                required_card_count=configured_cut,
            )
            for point in brief.novelty_points
            if counts[point.point_id] < configured_cut
        ]
        decisions, failures = plan_recovery(
            paper_id=state["paper"].paper_id, run_id=state.get("run_id"),
            points=brief.novelty_points, results=state.get("task_research_results", []),
            reviews=state.get("novelty_reviews", []), insufficient=insufficient,
            round_number=state.get("rounds", 1), max_rounds=self.config.max_rounds,
            provider_order=self.config.recovery_provider_order,
            history=state.get("recovery_history", []),
        )
        failures = list({event.event_id:event for event in [*state.get("failure_events", []), *failures]}.values())
        from ..persistence import _atomic_write_json, paper_workspace
        _atomic_write_json(paper_workspace(state["paper"], output_root=self.output_root) /
            f"recovery-round-{state.get('rounds', 1)}.json", {
                "schema_version": 1,
                "failures": [f.model_dump(mode="json") for f in failures],
                "decisions": [d.model_dump(mode="json") for d in decisions],
                "semantic_verdict_created": False,
            })
        return {"insufficient_final_evidence_points": insufficient,
                "recovery_decisions": decisions, "failure_events": failures}

    async def _validate_synthesis_input(
        self, state: NoveltyState
    ) -> dict[str, Any]:
        """Gate A: validate Reviews; leave Validator's accepted Cards intact."""
        result = validate_review_integrity(
            state.get("novelty_reviews", []),
            evidence_cards=state.get("evidence_cards", []),
            evidence=state.get("raw_evidence", []),
            tasks=state.get("all_research_tasks", []),
            novelty_points=state.get("novelty_points", []),
            paper_id=state["paper"].paper_id,
            reference_store=self.reference_store,
        )
        audit = result.audit()
        persist_novelty_reviews(
            state["paper"], result.reviews, output_root=self.output_root,
            card_reviews=state.get("review_card_results"), integrity=audit,
        )
        return {
            "novelty_reviews": list(result.reviews),
            "synthesis_integrity": audit,
            "issues": [WorkflowIssue(
                node="validate_synthesis_input", code="review_integrity",
                message=f"Review {item['novelty_point_id']}: {'; '.join(item['reasons'])}",
                severity=IssueSeverity.ERROR,
            ) for item in result.rejected],
        }

    async def _route_after_evidence_sufficiency_check(
        self, state: NoveltyState
    ) -> str:
        """根据数量检查事实和轮次上限选择补检或汇总。"""
        if "recovery_decisions" in state:
            actionable = any(d.action not in {RecoveryAction.STOP, RecoveryAction.MANUAL, RecoveryAction.NONE}
                             for d in state["recovery_decisions"])
            return "supplement" if actionable and state.get("rounds", 0) < self.config.max_rounds else "synthesize"
        if state.get("insufficient_final_evidence_points") and state.get("rounds", 0) < self.config.max_rounds:
            return "supplement"
        return "synthesize"

    async def _plan_supplement(self, state: NoveltyState) -> dict[str, Any]:
        next_round = state.get("rounds", 1) + 1
        if "recovery_decisions" in state:
            return self._targeted_supplement(state, next_round)
        try:
            brief_value = self.services.coordinator.plan_supplement(
                state["paper"],
                brief=state["brief"],
                existing_evidence=state.get("evidence_cards", []),
                insufficient_final_evidence_points=state.get(
                    "insufficient_final_evidence_points", []
                ),
                attempt=next_round,
            )
            brief = NoveltyBrief.model_validate(await _resolve(brief_value))
        except (ValidationError, TypeError, ValueError) as exc:
            raise WorkflowExecutionError(f"Coordinator 未生成合法补充任务：{exc}") from exc

        existing_tasks = state.get("all_research_tasks", [])
        task_by_key = {_task_key(task): task for task in existing_tasks}
        for task in brief.research_tasks:
            task_by_key[_task_key(task)] = task

        all_tasks = list(task_by_key.values())
        return {
            "brief": brief,
            "research_tasks": list(brief.research_tasks),
            "all_research_tasks": all_tasks,
            "rounds": next_round,
        }

    def _targeted_supplement(self, state, next_round):
        tasks, directives, plans = [], {}, {}
        decisions = [d for d in state["recovery_decisions"]
                     if d.action not in {RecoveryAction.STOP, RecoveryAction.MANUAL, RecoveryAction.NONE}]
        old_tasks = state.get("all_research_tasks", state["brief"].research_tasks)
        direct_actions = {RecoveryAction.READ_ARTIFACT, RecoveryAction.FETCH_FULLTEXT,
                          RecoveryAction.CHANGE_PROVIDER, RecoveryAction.RETRY_REQUEST}
        for directive in decisions:
            previous_tasks = [t for t in old_tasks if t.novelty_point_id == directive.point_id]
            languages = list(dict.fromkeys(t.language for t in previous_tasks)) or ["en"]
            if directive.action in direct_actions:
                languages = languages[:1]
            previous_plan = next((p for p in reversed(state.get("search_plans", []))
                                  if p.novelty_point_id == directive.point_id), None)
            for index, language in enumerate(languages, 1):
                task = ResearchTask(task_id=f"T-R{next_round}-{index}", novelty_point_id=directive.point_id,
                    task_type=directive.action.value, language=language, attempt=next_round,
                    description=directive.reason + "\n" + json.dumps({
                        "missing_feature_ids":directive.missing_feature_ids,
                        "missing_aspects":directive.missing_aspects,
                    }, ensure_ascii=False))
                key = _recovery_key(task.novelty_point_id, task.task_id)
                tasks.append(task)
                directives[key] = directive
                if directive.action in direct_actions and previous_plan is not None:
                    plans[key] = previous_plan.model_copy(update={"task_id":task.task_id})
        task_by_key = {_task_key(t):t for t in [*old_tasks, *tasks]}
        return {"brief":state["brief"].model_copy(update={"research_tasks":tasks}),
                "research_tasks":tasks, "all_research_tasks":list(task_by_key.values()),
                "rounds":next_round, "recovery_directives":directives, "recovery_search_plans":plans,
                "recovery_history":[*state.get("recovery_history", []), *decisions]}

    async def _synthesize_report(self, state: NoveltyState) -> dict[str, Any]:
        integrity_rejected_ids = set(
            state.get("integrity_rejected_card_ids", [])
        )
        rejected_reasons = [
            f"{item.card_id}: {item.reason}"
            for item in state.get("rejected_evidence", [])
            if item.card_id not in integrity_rejected_ids
        ]
        try:
            report_value = self.services.coordinator.synthesize(
                state["paper"],
                brief=state["brief"],
                evidence=state.get("evidence_cards", []),
                novelty_reviews=state.get("novelty_reviews", []),
                rejected_evidence=rejected_reasons,
                insufficient_final_evidence_points=state.get(
                    "insufficient_final_evidence_points", []
                ),
            )
            draft = NoveltyReport.model_validate(await _resolve(report_value))
            report = bind_reviews_to_report(
                draft,
                novelty_reviews=state.get("novelty_reviews", []),
                novelty_points=state.get("novelty_points", []),
                evidence_cards=state.get("evidence_cards", []),
            )
        except (ValidationError, TypeError, ValueError) as exc:
            raise WorkflowExecutionError(f"Coordinator 未生成合法 NoveltyReport：{exc}") from exc

        if report.paper_id != state["paper"].paper_id:
            raise WorkflowExecutionError("NoveltyReport.paper_id 与输入论文不一致")
        report = bind_search_coverage_to_report(
            report,
            novelty_points=state.get("novelty_points", []),
            task_research_results=state.get("task_research_results", []),
        )
        scope_limitations = [issue.message for issue in state.get("issues", [])
                             if issue.code in {"point_deduplication_unresolved", "point_scope_below_target", "point_coverage_unresolved"}]
        if scope_limitations:
            report = report.model_copy(update={"limitations": list(dict.fromkeys(
                [*report.limitations, *scope_limitations]
            ))})
        partial_reviews = []
        accepted_card_points = {card.card_id:card.novelty_point_id for card in state.get("evidence_cards", [])}
        for attempt in state.get("review_summary_attempts", []):
            if (attempt.get("review") or {}).get("incomplete_reason") not in {"technical_error", "budget_exhausted"}:
                continue
            for row in attempt.get("partial_card_results", []):
                if (row.get("status") == "completed" and row.get("review")
                        and accepted_card_points.get(row.get("card_id")) == attempt["point_id"]):
                    partial_reviews.append(PartialCardReview(card_id=row["card_id"],
                        novelty_point_id=attempt["point_id"], review=row["review"], summary_status="failed"))
        limitations = list(report.limitations)
        if partial_reviews:
            limitations.append("部分单卡核验已保存，但查新点汇总未完成；局部核验不能替代最终裁定。")
        terminal = [d for d in state.get("recovery_decisions", []) if d.action == RecoveryAction.STOP]
        limitations += [f"{d.point_id} 恢复停止：{d.reason}" for d in terminal]
        report = report.model_copy(update={
            "partial_card_reviews": partial_reviews,
            "execution_failures": state.get("failure_events", []),
            "recovery_decisions": [*state.get("recovery_history", []), *state.get("recovery_decisions", [])],
            "point_coverage": state.get("point_coverage", {}),
            "limitations": list(dict.fromkeys(limitations)),
        })
        report = bind_point_lifecycle_to_report(report, state=state)
        return {"report": report}

    async def _validate_report_integrity(
        self, state: NoveltyState
    ) -> dict[str, Any]:
        """Gate B: observe report reference failures without blocking output."""

        result = validate_report_integrity(
            state["report"],
            novelty_points=state.get("novelty_points", []),
            evidence_cards=state.get("evidence_cards", []),
            novelty_reviews=state.get("novelty_reviews", []),
            evidence=state.get("raw_evidence", []),
            reference_store=self.reference_store,
        )
        return {"report_integrity": result.audit()}

    async def _persist_report(self, state: NoveltyState) -> dict[str, Any]:
        """Persist the unchanged report after the non-blocking output gate."""

        persist_report(
            state["paper"], state["report"], output_root=self.output_root
        )
        return {}

    async def _render_report(self, state: NoveltyState) -> dict[str, Any]:
        """在结构化报告落盘后生成默认 Markdown 报告。"""

        try:
            path = render_report(
                output_format="markdown",
                paper_name=state["paper"].paper_id,
                output_root=self.output_root,
            )
        except (OSError, ValueError, RuntimeError) as exc:
            raise WorkflowExecutionError(f"Markdown 报告渲染失败：{exc}") from exc
        return {"rendered_report_path": str(path)}

    async def arun(
        self,
        paper: PaperInput | dict[str, Any],
        *,
        run_identity: Mapping[str, Any] | None = None,
        progress_callback: ProgressCallback | None = None,
    ) -> NoveltyRunResult:
        """异步执行一次完整查新工作流。"""

        paper_input = PaperInput.model_validate(paper)
        run_id = f"run-{uuid.uuid4().hex}"
        self.last_runtime_run_id = run_id
        self.last_rendered_report_path = None
        initial: NoveltyState = {
            "paper": paper_input,
            "run_id": run_id,
            "research_tasks": [],
            "all_research_tasks": [],
            "task_research_results": [],
            "raw_evidence": [],
            "raw_evidence_cards": [],
            "search_plans": [],
            "validator_accepted_cards": [],
            "evidence_cards": [],
            "rejected_evidence": [],
            "review_decisions": [],
            "novelty_reviews": [],
            "insufficient_final_evidence_points": [],
            "issues": [],
            "integrity_rejected_card_ids": [],
            "rounds": 0,
        }
        model_client = getattr(self.services.task_researcher, "model_client", None)
        profile = getattr(model_client, "profile", None)
        tool_registry = getattr(self.services.task_researcher, "tools", None)
        manager = RuntimeArtifactManager(
            paper_input.paper_id,
            config=self.config.runtime_debug,
            run_id=run_id,
            run_identity=run_identity,
            runtime_config=self.runtime_config or {
                "workflow": {
                    "max_rounds": self.config.max_rounds,
                    "max_concurrency": self.config.max_concurrency,
                    "min_final_evidence_cards_per_point": (
                        self.config.min_final_evidence_cards_per_point
                    ),
                    "candidate_limit_per_task": self.config.candidate_limit_per_task,
                    "recovery_provider_order": list(self.config.recovery_provider_order),
                }
            },
            model_provider=getattr(profile, "provider", None),
            model_name=getattr(profile, "model", None),
            enabled_tools=getattr(tool_registry, "names", ()),
            stage_names=_STAGE_NAMES,
        )
        manager.activate()
        progress_token = _progress_callback.set(progress_callback)
        try:
            final = await self.graph.ainvoke(
                initial, config={"max_concurrency": self.config.max_concurrency}
            )
            if "brief" not in final or "report" not in final:
                raise WorkflowExecutionError("工作流结束时缺少 Brief 或 Report")
            result = NoveltyRunResult(
                failure_events=final.get("failure_events", []),
                recovery_decisions=[*final.get("recovery_history", []), *final.get("recovery_decisions", [])],
                brief=final["brief"],
                evidence_cards=final.get("evidence_cards", []),
                rejected_evidence=final.get("rejected_evidence", []),
                novelty_reviews=final.get("novelty_reviews", []),
                insufficient_final_evidence_points=final.get(
                    "insufficient_final_evidence_points", []
                ),
                issues=final.get("issues", []),
                rounds=final.get("rounds", 0),
                report=final["report"],
            )
            self.last_rendered_report_path = final.get("rendered_report_path")
        except (KeyboardInterrupt, asyncio.CancelledError) as exc:
            manager.finish_run("INTERRUPTED", error=exc)
            raise
        except Exception as exc:
            manager.finish_run("FAILED", error=exc)
            raise
        else:
            manager.finish_run("SUCCESS")
            return result
        finally:
            _progress_callback.reset(progress_token)
            manager.deactivate()

    def run(
        self,
        paper: PaperInput | dict[str, Any],
        *,
        run_identity: Mapping[str, Any] | None = None,
    ) -> NoveltyRunResult:
        """同步入口；异步应用应直接调用 arun。"""

        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(self.arun(paper, run_identity=run_identity))
        raise RuntimeError("检测到正在运行的事件循环，请改用 await workflow.arun(...) ")


def _recovery_key(point_id: str, task_id: str) -> str:
    return json.dumps([point_id, task_id], ensure_ascii=False)


def _task_key(task: ResearchTask) -> tuple[str, str]:
    return task.novelty_point_id, task.task_id


def _require_complete_search_plans(
    tasks: list[ResearchTask], plans: list[SearchPlan]
) -> None:
    """Fail closed when planning fan-in does not exactly cover current tasks."""

    expected = {_task_key(task) for task in tasks}
    actual = {(plan.novelty_point_id, plan.task_id) for plan in plans}
    missing = sorted(expected - actual)
    unexpected = sorted(actual - expected)
    duplicate_count = len(plans) - len(actual)
    if missing or unexpected or duplicate_count:
        detail = {
            "expected_task_count": len(tasks),
            "search_plan_count": len(plans),
            "missing_task_keys": [list(key) for key in missing],
            "unexpected_plan_keys": [list(key) for key in unexpected],
            "duplicate_plan_count": duplicate_count,
        }
        raise WorkflowExecutionError(
            "SearchPlan completeness check failed: "
            + json.dumps(detail, ensure_ascii=False, sort_keys=True)
        )
