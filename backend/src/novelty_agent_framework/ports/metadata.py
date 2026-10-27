"""Shared metadata retrieval contracts; no authenticity decision in this layer."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from .doi import DoiMetadataRecord
from ..schemas.failures import FailureCode


@dataclass(frozen=True)
class MetadataQuery:
    doi: str | None = None
    title: str | None = None
    authors: tuple[str, ...] = ()
    year: int | None = None
    venue: str | None = None
    citation: str | None = None
    limit: int = 5

    def __post_init__(self):
        for name in ("doi", "title", "venue", "citation"):
            value = getattr(self, name)
            if value is not None:
                if not isinstance(value, str):
                    raise ValueError(f"{name} must be a string")
                object.__setattr__(self, name, value.strip() or None)
        if isinstance(self.authors, str) or any(not isinstance(a, str) for a in self.authors):
            raise ValueError("authors must be a sequence of names")
        object.__setattr__(self, "authors", tuple(a.strip() for a in self.authors if a.strip()))
        if self.year is not None and (type(self.year) is not int or not 1 <= self.year <= 9999):
            raise ValueError("year must be an integer between 1 and 9999")
        if type(self.limit) is not int or not 1 <= self.limit <= 10:
            raise ValueError("limit must be between 1 and 10")
        if not any((self.doi, self.title, self.authors, self.citation)):
            raise ValueError("provide a DOI, title, authors, or citation")


@dataclass(frozen=True)
class ProviderMetadataResult:
    source_id: str
    query: MetadataQuery
    status: Literal["found", "not_found", "unsupported", "error"]
    coverage: str
    records: tuple[DoiMetadataRecord, ...] = ()
    http_status: int | None = None
    error_type: str | None = None
    truncated: bool = False
    failure_code: FailureCode | None = None


@dataclass(frozen=True)
class MetadataFieldValue:
    value: Any
    record_refs: tuple[str, ...]


@dataclass(frozen=True)
class MetadataCard:
    """Merged facts about ONE candidate; unknown/conflicting fields remain empty.

    Source record refs point to independent responses, not to model claims.
    Different DOI identities are never merged merely because titles resemble.
    """

    candidate_id: str
    metadata: DoiMetadataRecord
    field_sources: dict[str, tuple[str, ...]] = field(default_factory=dict)
    conflicts: dict[str, tuple[MetadataFieldValue, ...]] = field(default_factory=dict)
    records: dict[str, DoiMetadataRecord] = field(default_factory=dict)
    urls: tuple[str, ...] = ()
    missing_fields: tuple[str, ...] = ()


@dataclass(frozen=True)
class MetadataRetrievalResult:
    query: MetadataQuery
    providers: tuple[ProviderMetadataResult, ...]
    cards: tuple[MetadataCard, ...]
    status: Literal["complete", "partial", "failed", "unsupported"]

    @property
    def card(self) -> MetadataCard | None:
        """A single unambiguous candidate, if present; no first-hit selection."""
        return self.cards[0] if len(self.cards) == 1 else None


class MetadataRetrievalTool(Protocol):
    source_id: str

    def retrieve(self, query: MetadataQuery) -> ProviderMetadataResult:
        ...


class MetadataRetrieval(Protocol):
    def retrieve(self, query: MetadataQuery) -> MetadataRetrievalResult:
        ...

    async def aretrieve(self, query: MetadataQuery) -> MetadataRetrievalResult:
        ...
