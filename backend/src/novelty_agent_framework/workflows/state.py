"""LangGraph 状态和运行时依赖。"""

from __future__ import annotations

from dataclasses import dataclass, field
from operator import add
from typing import Annotated, TypedDict

from ..schemas import (
    EvidenceCard,
    EvidenceReviewDecision,
    Evidence,
    InsufficientFinalEvidence,
    NoveltyBrief,
    NoveltyPoint,
    NoveltyPointReview,
    NoveltyReport,
    PaperInput,
    RejectedEvidence,
    ResearchTask,
    SearchPlan,
    TaskResearchResult,
    WorkflowIssue,
)
from ..ports import (
    EvidenceValidator,
    EvidenceReviewer,
    NoveltyCoordinator,
    NoveltyPointExtractor,
    SearchPlanner,
    TaskResearcher,
)
from ..core.runtime_artifacts import RuntimeDebugConfig
from ..schemas.research import TargetPaperIdentity
from ..schemas.failures import FailureEvent, RecoveryDecision


class NoveltyState(TypedDict, total=False):
    """一次查新任务在图中的共享状态。"""

    paper: PaperInput
    point_coverage: dict
    novelty_points: list[NoveltyPoint]
    brief: NoveltyBrief
    research_tasks: list[ResearchTask]
    all_research_tasks: list[ResearchTask]
    run_id: str
    subject_paper_id: str
    current_point: NoveltyPoint
    current_task: ResearchTask
    current_search_plan: SearchPlan
    target_identity: TargetPaperIdentity
    search_plans: Annotated[list[SearchPlan], add]
    task_research_results: Annotated[list[TaskResearchResult], add]
    raw_evidence: Annotated[list[Evidence], add]
    raw_evidence_cards: Annotated[list[EvidenceCard], add]
    validator_accepted_cards: list[EvidenceCard]
    evidence_cards: list[EvidenceCard]
    rejected_evidence: list[RejectedEvidence]
    review_decisions: list[EvidenceReviewDecision]
    novelty_reviews: list[NoveltyPointReview]
    insufficient_final_evidence_points: list[InsufficientFinalEvidence]
    failure_events: list[FailureEvent]
    recovery_decisions: list[RecoveryDecision]
    recovery_history: list[RecoveryDecision]
    recovery_directives: dict[str, RecoveryDecision]
    recovery_search_plans: dict[str, SearchPlan]
    current_recovery: RecoveryDecision
    review_summary_attempts: list[dict]
    issues: Annotated[list[WorkflowIssue], add]
    rounds: int
    report: NoveltyReport
    evidence_integrity: dict
    review_card_results: list[dict] | None
    synthesis_integrity: dict
    integrity_rejected_card_ids: Annotated[list[str], add]
    report_integrity: dict
    rendered_report_path: str
    rendered_report_paths: dict[str, str]


@dataclass(frozen=True)
class NoveltyWorkflowConfig:
    """只包含工作流级控制参数，不包含具体 Prompt。"""

    max_rounds: int = 2
    max_concurrency: int = 4
    min_final_evidence_cards_per_point: int = 1
    candidate_limit_per_task: int = 8
    recovery_provider_order: tuple[str, ...] = ()
    runtime_debug: RuntimeDebugConfig = field(default_factory=RuntimeDebugConfig)

    def __post_init__(self) -> None:
        if self.max_rounds < 1:
            raise ValueError("max_rounds 必须至少为 1")
        if self.max_concurrency < 1:
            raise ValueError("max_concurrency 必须至少为 1")
        if self.min_final_evidence_cards_per_point < 1:
            raise ValueError("min_final_evidence_cards_per_point 必须至少为 1")
        if self.candidate_limit_per_task < 1:
            raise ValueError("candidate_limit_per_task 必须至少为 1")


@dataclass(frozen=True)
class NoveltyWorkflowServices:
    """主图只依赖全局角色和任务级 Researcher。"""

    coordinator: NoveltyCoordinator
    task_researcher: TaskResearcher
    search_planner: SearchPlanner
    point_extractor: NoveltyPointExtractor | None = None
    validator: EvidenceValidator | None = None
    reviewer: EvidenceReviewer | None = None
