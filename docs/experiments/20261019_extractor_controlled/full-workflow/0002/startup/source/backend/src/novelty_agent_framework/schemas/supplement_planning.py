"""Versioned, compact input for task-level Coordinator planning."""
from __future__ import annotations

import hashlib
from pathlib import PurePosixPath
from typing import Literal
from typing import Any

from pydantic import Field, model_validator

from .domain import InsufficientFinalEvidence, NoveltyBrief, NoveltyPoint, ResearchTask, StrictModel
from .failures import FailureEvent, RecoveryAction, RecoveryAttempt, RecoveryDecision


class PlanningPaper(StrictModel):
    paper_id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    abstract: str


class PlanningFeature(StrictModel):
    feature_id: str = Field(pattern=r"^F[1-9][0-9]*$")
    text: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class PlanningPoint(NoveltyPoint):
    feature_catalog: list[PlanningFeature]

    @model_validator(mode="after")
    def consistent_features(self):
        expected = [(f"F{i}", text, hashlib.sha256(text.encode()).hexdigest())
                    for i, text in enumerate(self.technical_features, 1)]
        if [(f.feature_id, f.text, f.sha256) for f in self.feature_catalog] != expected:
            raise ValueError("feature_catalog must match the approved point's ordered features")
        return self


class PlanningBrief(StrictModel):
    paper_summary: str
    research_problem: str
    keywords_zh: list[str]
    keywords_en: list[str]
    novelty_points: list[PlanningPoint] = Field(min_length=1)


class PlanningEvidence(StrictModel):
    card_id: str = Field(min_length=1)
    novelty_point_id: str = Field(min_length=1)
    document_title: str
    main_contribution: str
    overlaps: list[str]
    differences: list[str]


class PlanningCountGap(InsufficientFinalEvidence):
    additional_cards_needed: int = Field(ge=1)

    @model_validator(mode="after")
    def consistent_gap(self):
        if self.additional_cards_needed != self.required_card_count - self.valid_card_count:
            raise ValueError("additional_cards_needed must equal required minus valid")
        return self


class PersistedPlanningMaterial(StrictModel):
    """Verified full record, addressed relative to the configured output root.

    SHA-256 covers the entire file, not just the object at json_pointer.
    An index is a locator, not a claim that the model has read its contents.
    """
    material_id: str = Field(pattern=r"^M[1-9][0-9]*$")
    kind: Literal["paper", "novelty_point", "evidence_card", "task_result", "review", "recovery_decision"]
    point_id: str | None = None
    task_id: str | None = None
    card_id: str | None = None
    trigger_code: Literal["evidence.missing", "review.missing_features", "coverage.no_match"] | None = None
    attempt: int | None = Field(default=None, ge=1)
    path: str = Field(min_length=1)
    json_pointer: str = Field(pattern=r"^(?:/(?:[^~/]|~[01])*)*$")
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def valid_locator(self):
        path = PurePosixPath(self.path)
        if path.is_absolute() or ".." in path.parts or "\\" in self.path or ":" in self.path:
            raise ValueError("material path must stay relative to output_root")
        if (self.kind == "paper") != (self.point_id is None):
            raise ValueError("point_id is required for point-scoped materials only")
        if ((self.kind == "task_result") != (self.task_id is not None)
                or (self.kind == "task_result") != (self.attempt is not None)):
            raise ValueError("task_result requires task_id and attempt")
        if (self.kind == "evidence_card") != (self.card_id is not None):
            raise ValueError("evidence_card requires card_id")
        if (self.kind == "recovery_decision") != (self.trigger_code is not None):
            raise ValueError("recovery_decision material must bind its trigger code")
        return self


class SupplementPlanningInput(StrictModel):
    schema_version: Literal[2] = 2
    run_id: str | None = None  # Direct callers without runtime context may omit it.
    paper: PlanningPaper
    brief: PlanningBrief
    recovery_decisions: list[RecoveryDecision] = Field(min_length=1)
    existing_evidence: list[PlanningEvidence]
    insufficient_final_evidence_points: list[PlanningCountGap]
    research_languages: list[Literal["zh", "en"]] = Field(min_length=1)
    attempt: int = Field(ge=2)
    research_history: list[RecoveryAttempt]
    material_index: list[PersistedPlanningMaterial] = Field(default_factory=list)

    @property
    def subject_paper_id(self) -> str:
        return self.paper.paper_id

    @model_validator(mode="after")
    def valid_scope(self):
        points = {p.point_id: p for p in self.brief.novelty_points}
        decision_keys = {(d.point_id, tuple(d.trigger_codes)) for d in self.recovery_decisions}
        if (len(points) != len(self.brief.novelty_points) or
                len(decision_keys) != len(self.recovery_decisions) or
                points.keys() != {d.point_id for d in self.recovery_decisions}):
            raise ValueError("decisions must uniquely bind a trigger code and an approved point")
        for d in self.recovery_decisions:
            if len(d.trigger_codes) != 1:
                raise ValueError("each pending decision must belong to exactly one trigger workflow")
            if (d.action not in {RecoveryAction.RESEARCH_GAP, RecoveryAction.REPLAN_QUERY}
                    or d.coordinator_status != "not_requested" or d.max_additional_attempts != 1):
                raise ValueError("only pending authorized task-level decisions may be planned")
            if d.round_number is not None and d.round_number != self.attempt - 1:
                raise ValueError("decision round must precede this attempt")
        if len(set(self.research_languages)) != len(self.research_languages):
            raise ValueError("research_languages must be unique")
        for item in [*self.existing_evidence, *self.insufficient_final_evidence_points]:
            if item.novelty_point_id not in points:
                raise ValueError("summary outside approved points")
        for h in self.research_history:
            if h.point_id not in points or h.attempt >= self.attempt:
                raise ValueError("history must belong to approved points and earlier attempts")
        if len({r.material_id for r in self.material_index}) != len(self.material_index):
            raise ValueError("material IDs must be unique within this input")
        for ref in self.material_index:
            if ref.point_id is not None and ref.point_id not in points:
                raise ValueError("material outside approved points")
            if ref.kind == "recovery_decision" and (ref.point_id, (ref.trigger_code,)) not in decision_keys:
                raise ValueError("decision material outside this trigger workflow")
            if ref.kind == "evidence_card" and not any(
                c.card_id == ref.card_id and c.novelty_point_id == ref.point_id for c in self.existing_evidence
            ):
                raise ValueError("material must reference an input valid card")
            if ref.kind == "task_result" and not any(
                (h.point_id, h.task_id, h.attempt) == (ref.point_id, ref.task_id, ref.attempt)
                for h in self.research_history
            ):
                raise ValueError("material must reference an input history task")
        return self


class CoordinatorDecisionDraft(StrictModel):
    """Model-owned planning fields; runtime facts and budgets are deliberately absent."""
    point_id: str = Field(min_length=1)
    action: Literal["research_gap", "replan_query"]
    reason: str = Field(min_length=1)
    basis_read_ids: list[str] = Field(default_factory=list)
    research_tasks: list[ResearchTask] = Field(min_length=1)


class CoordinatorRecoveryDraft(StrictModel):
    trigger_code: Literal["evidence.missing", "review.missing_features", "coverage.no_match"]
    decisions: list[CoordinatorDecisionDraft] = Field(min_length=1)


class SupplementPlanningResult(StrictModel):
    """Validated LLM decisions, their bound tasks, and the observed execution audit."""
    brief: NoveltyBrief
    recovery_decisions: list[RecoveryDecision]
    failure_events: list[FailureEvent] = Field(default_factory=list)
    harness_audit: dict[str, Any] = Field(default_factory=dict)
