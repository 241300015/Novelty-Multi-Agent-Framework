"""Exact DOI lookup contracts at the same boundary as SearchTool/MetadataTool."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol


@dataclass(frozen=True)
class DoiMetadataRecord:
    """Observed registration or bibliographic facts; missing fields stay absent."""

    source_id: str
    doi: str | None
    record_kind: Literal["handle", "bibliographic"]
    title: str | None = None
    authors: tuple[str, ...] = ()
    year: int | None = None
    url: str | None = None
    publisher: str | None = None
    resource_type: str | None = None
    abstract: str | None = None
    raw_metadata: dict[str, Any] = field(default_factory=dict)
    venue: str | None = None
    volume: str | None = None
    issue: str | None = None
    pages: str | None = None
    page_count: int | None = None


@dataclass(frozen=True)
class DoiLookupResult:
    """A miss is scoped to this API's coverage, never a global validity verdict.

    Transport, HTTP and response-contract failures raise exceptions; they cannot
    be represented as not_found. A Handle record is not bibliographic metadata.
    """

    source_id: str
    requested_doi: str
    status: Literal["found", "not_found"]
    coverage: Literal["handle_registry", "crossref_metadata", "datacite_findable"]
    http_status: int
    record: DoiMetadataRecord | None = None


class DoiLookupTool(Protocol):
    """Callable exact-DOI capability, independent of agents and validation policy."""

    source_id: str

    def lookup(self, doi: str) -> DoiLookupResult:
        ...

    def close(self) -> None:
        ...

    def __enter__(self) -> DoiLookupTool:
        ...

    def __exit__(self, *exc_info: Any) -> None:
        ...
