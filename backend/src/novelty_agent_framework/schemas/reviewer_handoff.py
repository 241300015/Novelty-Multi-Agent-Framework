"""Point synthesis inputs, deliberately separate from persisted reading records."""
from __future__ import annotations

from typing import Literal

from pydantic import Field

from .domain import (
    StrictModel, FeatureComparison, RelevantWork, SupplementRequest,
    ReviewStatus, CardSupportStatus,
)


class SelectedEvidenceHandle(StrictModel):
    evidence_id: str
    work_id: str
    card_id: str


class CardJudgmentHandoff(StrictModel):
    review_status: ReviewStatus
    support_status: CardSupportStatus | None = None
    support_reason: str
    highly_relevant_works: list[RelevantWork] = Field(default_factory=list)
    feature_comparisons: list[FeatureComparison] = Field(default_factory=list)
    supplement_request: SupplementRequest | None = None
    incomplete_reason: str | None = None
    limitations: list[str] = Field(default_factory=list)


class CardSummaryHandoff(StrictModel):
    contract_version: Literal["card_judgment_v1"] = "card_judgment_v1"
    index: int
    card_id: str
    novelty_point_id: str
    document_title: str
    status: Literal["completed", "failed"]
    review: CardJudgmentHandoff | None = None
    selected_evidence: list[SelectedEvidenceHandle] = Field(default_factory=list)
    error: str | None = None


class SummaryRecheckTarget(StrictModel):
    card_id: str = Field(min_length=1)
    work_id: str = Field(min_length=1)
    feature_id: str = Field(min_length=1)
    evidence_ids: list[str] = Field(min_length=1)
    question: str = Field(min_length=1)


class SummaryRecheckRequest(StrictModel):
    """Alternative to a final draft; the runtime resolves existing selected evidence."""
    recheck: SummaryRecheckTarget
