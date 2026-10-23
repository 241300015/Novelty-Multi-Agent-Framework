"""任务级 Researcher 子工作流的数据契约。"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import Field, StringConstraints, model_validator

from .domain import EvidenceCard, NoveltyPoint, ResearchTask, SearchPlan, StrictModel
from .references import Evidence, ExternalIdentifier, ResearchBundle, SearchExecution
from .research_tools import ResearchFinishDraft, ReferenceReadResult, EvidenceCardBuildRejection
from .failures import FailureEvent, RecoveryDecision, CorrectionRecord

NonEmptyStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class TargetPaperIdentity(StrictModel):
    title: NonEmptyStr
    alternate_titles: list[NonEmptyStr] = Field(default_factory=list)
    authors: list[NonEmptyStr] = Field(default_factory=list)
    identifiers: list[ExternalIdentifier] = Field(default_factory=list)


class TaskResearchRequest(StrictModel):
    subject_paper_id: NonEmptyStr
    run_id: NonEmptyStr
    novelty_point: NoveltyPoint
    research_task: ResearchTask
    search_plan: SearchPlan
    target_identity: TargetPaperIdentity | None = None
    recovery: RecoveryDecision | None = None

    @model_validator(mode="after")
    def bind_task(self) -> TaskResearchRequest:
        if self.research_task.novelty_point_id != self.novelty_point.point_id:
            raise ValueError("research_task must belong to novelty_point")
        if self.search_plan.task_id != self.research_task.task_id:
            raise ValueError("search_plan must belong to research_task")
        if self.search_plan.novelty_point_id != self.research_task.novelty_point_id:
            raise ValueError("search_plan must belong to research_task novelty_point")
        if self.search_plan.novelty_point_id != self.novelty_point.point_id:
            raise ValueError("search_plan must belong to novelty_point")
        if self.recovery is not None and self.recovery.point_id != self.novelty_point.point_id:
            raise ValueError("recovery directive has wrong point binding")
        return self


class NoveltyPointReviewRequest(StrictModel):
    """Reviewer 单次调用的查新点级、受约束输入。"""

    subject_paper_id: NonEmptyStr
    novelty_point: NoveltyPoint
    tasks: list[ResearchTask] = Field(default_factory=list)
    cards: list[EvidenceCard] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_bindings(self) -> "NoveltyPointReviewRequest":
        point_id = self.novelty_point.point_id
        if any(task.novelty_point_id != point_id for task in self.tasks):
            raise ValueError("all tasks must belong to novelty_point")
        if any(card.novelty_point_id != point_id for card in self.cards):
            raise ValueError("all cards must belong to novelty_point")
        if any(item.novelty_point_id != point_id for item in self.evidence):
            raise ValueError("all evidence must belong to novelty_point")
        evidence_ids = {item.evidence_id for item in self.evidence}
        unresolved = {
            evidence_id
            for card in self.cards
            for evidence_id in card.evidence_ids
            if evidence_id not in evidence_ids
        }
        if unresolved:
            raise ValueError(
                "card evidence_ids are absent from request evidence: "
                + ", ".join(sorted(unresolved))
            )
        return self


class CallToolAction(StrictModel):
    action: Literal["call_tool"]
    tool_name: NonEmptyStr
    arguments: dict[str, Any] = Field(default_factory=dict)


class FinishResearchAction(StrictModel):
    action: Literal["finish"]
    draft: ResearchFinishDraft

    @property
    def cards(self):
        """Read-only bridge for the not-yet-migrated TaskResearcherWorkflow."""

        return self.draft.cards


ResearcherAction = Annotated[
    CallToolAction | FinishResearchAction, Field(discriminator="action")
]


class TaskResearchStatus(StrEnum):
    COMPLETED = "completed"
    PARTIAL = "partial"
    FAILED = "failed"


class ResearcherToolObservation(StrictModel):
    tool_name: NonEmptyStr
    arguments: dict[str, Any] = Field(default_factory=dict)
    succeeded: bool
    summary: str = ""
    payload: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None
    elapsed_ms: int = Field(default=0, ge=0)


class CandidateAuditRecord(StrictModel):
    """Observed candidate disposition, separate from trusted evidence."""

    namespace: Literal["research_reference", "subject_reference"]
    source_record_id: str | None = None
    work_id: str | None = None
    title: str | None = None
    artifact_ids: list[str] = Field(default_factory=list)
    read_ids: list[str] = Field(default_factory=list)
    card_ids: list[str] = Field(default_factory=list)
    status: Literal[
        "not_read", "acquisition_unavailable", "read_without_card",
        "read_task_interrupted", "card_produced", "excluded",
    ]
    reason: NonEmptyStr
    excluded_reason: Literal["target_paper"] | None = None
    identity_match: NonEmptyStr | None = None


class TaskResearchResult(StrictModel):
    task_id: NonEmptyStr
    novelty_point_id: NonEmptyStr
    status: TaskResearchStatus
    research_bundles: list[ResearchBundle] = Field(default_factory=list)
    read_results: list[ReferenceReadResult] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)
    evidence_cards: list[EvidenceCard] = Field(default_factory=list)
    search_executions: list[SearchExecution] = Field(default_factory=list)
    candidate_audit: list[CandidateAuditRecord] = Field(default_factory=list)
    # Attempt history only: initial rejections can have been repaired.
    card_build_rejections: list[EvidenceCardBuildRejection] = Field(default_factory=list)
    card_correction_rejections: list[EvidenceCardBuildRejection] = Field(default_factory=list)
    card_correction_failure: FailureEvent | None = None
    correction_records: list[CorrectionRecord] = Field(default_factory=list)
    correction_budget: dict[str, Any] = Field(default_factory=dict)
    validator_corrected_card_ids: list[str] = Field(default_factory=list)
    harness_diagnostics: list[FailureEvent] = Field(default_factory=list)
    execution_failures: list[FailureEvent] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    steps_used: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_bindings(self) -> TaskResearchResult:
        evidence_ids = {item.evidence_id for item in self.evidence}
        for item in self.evidence:
            if item.task_id != self.task_id or item.novelty_point_id != self.novelty_point_id:
                raise ValueError(f"evidence {item.evidence_id} has wrong task binding")
        for card in self.evidence_cards:
            if card.task_id != self.task_id or card.novelty_point_id != self.novelty_point_id:
                raise ValueError(f"evidence card {card.card_id} has wrong task binding")
            missing = set(card.evidence_ids) - evidence_ids
            if missing:
                raise ValueError(f"evidence card {card.card_id} references missing evidence {sorted(missing)}")
        return self
