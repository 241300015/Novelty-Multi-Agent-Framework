"""Contracts for deterministic paper-level reference bootstrapping."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated

from pydantic import Field, StringConstraints, model_validator

from .domain import StrictModel

NonEmptyStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class ResolutionStatus(StrEnum):
    RESOLVED = "resolved"
    AMBIGUOUS = "ambiguous"
    NOT_FOUND = "not_found"
    FAILED = "failed"


class ParsedCitation(StrictModel):
    title: str | None = None
    authors: list[str] = Field(default_factory=list)
    year: int | None = None
    doi: str | None = None
    url: str | None = None
    arxiv_id: str | None = None
    warnings: list[str] = Field(default_factory=list)


class ReferenceResolveAttempt(StrictModel):
    attempt_id: NonEmptyStr
    provider_id: NonEmptyStr
    method: NonEmptyStr
    query_or_identifier: NonEmptyStr
    status: NonEmptyStr
    candidate_work_ids: list[str] = Field(default_factory=list)
    selected_work_id: str | None = None
    error: str | None = None


class ReferenceBootstrapEntry(StrictModel):
    reference_id: NonEmptyStr
    ordinal: int = Field(ge=1)
    raw_reference: NonEmptyStr
    parsed: ParsedCitation
    resolution_status: ResolutionStatus
    resolved_work_id: str | None = None
    attempts: list[ReferenceResolveAttempt] = Field(default_factory=list)

    @model_validator(mode="after")
    def resolved_has_work(self) -> "ReferenceBootstrapEntry":
        if self.resolution_status == ResolutionStatus.RESOLVED and not self.resolved_work_id:
            raise ValueError("resolved entry requires resolved_work_id")
        return self


class ReferenceBootstrapManifest(StrictModel):
    schema_version: NonEmptyStr = "1.0"
    subject_paper_id: NonEmptyStr
    references_digest: str | None = None
    entries: list[ReferenceBootstrapEntry] = Field(default_factory=list)

    @property
    def bootstrap_ready(self) -> bool:
        return all(entry.attempts for entry in self.entries)
