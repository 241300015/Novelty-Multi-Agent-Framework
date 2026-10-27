"""Retrieve independent source facts and assemble metadata cards for Validator."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from dataclasses import replace
from typing import Any

from ..ports import (
    DoiMetadataRecord, MetadataCard, MetadataFieldValue, MetadataQuery,
    MetadataRetrievalResult, MetadataRetrievalTool, ProviderMetadataResult,
)
from .database_search.providers.doi_common import metadata_failure_code, normalize_doi

_FIELDS = ("doi", "title", "authors", "year", "url", "publisher", "venue", "resource_type", "abstract")


def _value_key(value):
    if isinstance(value, str):
        return " ".join(value.split()).casefold()
    if isinstance(value, tuple):
        return tuple(_value_key(item) for item in value)
    return value


def assemble_metadata_cards(results: Sequence[ProviderMetadataResult]) -> tuple[MetadataCard, ...]:
    """Only an equal DOI establishes a cross-record merge group.

    A title-only search can return multiple DOI identities: each stays separate.
    Missing identifiers never authorize title-similarity-based record stitching.
    """
    groups: dict[str, dict[str, DoiMetadataRecord]] = {}
    for result in results:
        for index, record in enumerate(result.records):
            ref = f"{result.source_id}:{index}"
            doi = normalize_doi(record.doi) if record.doi else None
            key = f"doi:{doi}" if doi else f"record:{ref}"
            groups.setdefault(key, {})[ref] = replace(record, doi=doi)
    cards = []
    for key, records in groups.items():
        values = {}
        field_sources = {}
        conflicts = {}
        urls = tuple(dict.fromkeys(record.url for record in records.values() if record.url))
        for name in _FIELDS:
            observed = {}
            for ref, record in records.items():
                value = getattr(record, name)
                if value is None or value == () or value == "":
                    continue
                normalized = value if name == "url" else _value_key(value)
                if normalized not in observed:
                    observed[normalized] = (value, [])
                observed[normalized][1].append(ref)
            if len(observed) == 1:
                value, refs = next(iter(observed.values()))
                values[name], field_sources[name] = value, tuple(refs)
            elif len(observed) > 1:
                # Multiple landing URLs are useful locations, not identity contradictions.
                # With no unique canonical URL keep url=None and expose all observed URLs.
                if name != "url":
                    conflicts[name] = tuple(MetadataFieldValue(value, tuple(refs))
                                            for value, refs in observed.values())
        kind = "bibliographic" if any(r.record_kind == "bibliographic" for r in records.values()) else "handle"
        metadata = DoiMetadataRecord(source_id="merged", doi=values.pop("doi", None),
                                     record_kind=kind, **values)
        missing = tuple(name for name in _FIELDS if getattr(metadata, name) in (None, (), ""))
        cards.append(MetadataCard(key, metadata, field_sources, conflicts, records, urls, missing))
    return tuple(cards)


class MetadataRetrievalService:
    """All configured providers return separately; this service never verifies a work.

    Each call performs at most one lookup/search per provider, with a bounded
    first page. No result cache, candidate selection, enrichment recursion, or
    extra retry layer. Provider transport policy owns physical request retries.
    """

    def __init__(self, providers: Sequence[MetadataRetrievalTool], *, owns_providers: bool = False):
        self.providers = tuple(providers)
        if not self.providers or len({p.source_id for p in self.providers}) != len(self.providers):
            raise ValueError("metadata providers must be nonempty and have unique source IDs")
        self._owns_providers = owns_providers
        self._closed = False

    def retrieve(self, query: MetadataQuery) -> MetadataRetrievalResult:
        if self._closed:
            raise RuntimeError("metadata retrieval service is closed")
        if query.doi:
            normalize_doi(query.doi)
        results = []
        for provider in self.providers:
            try:
                result = provider.retrieve(query)
                if (not isinstance(result, ProviderMetadataResult) or result.source_id != provider.source_id
                        or result.query != query or result.status not in {"found", "not_found", "error", "unsupported"}
                        or bool(result.records) != (result.status == "found")
                        or any(r.source_id != provider.source_id for r in result.records)):
                    raise ValueError("provider violated metadata response contract")
                for record in result.records:
                    if record.doi:
                        normalize_doi(record.doi)
                    if query.doi and (not record.doi or normalize_doi(record.doi) != normalize_doi(query.doi)):
                        raise ValueError("provider returned a different DOI")
                results.append(result)
            except Exception as exc:
                # An isolated provider failure cannot erase other providers' facts.
                # Do not serialize exception strings, which can expose custom client credentials.
                results.append(ProviderMetadataResult(
                    provider.source_id, query, "error", getattr(provider, "coverage", "unknown"),
                    http_status=getattr(exc, "status_code", None), error_type=type(exc).__name__,
                    failure_code=metadata_failure_code(exc, provider.source_id),
                ))
        statuses = [result.status for result in results]
        if all(status == "unsupported" for status in statuses):
            status = "unsupported"
        elif "error" in statuses:
            status = "partial" if any(s in {"found", "not_found"} for s in statuses) else "failed"
        else:
            status = "complete"  # execution complete, not proof of existence/completeness
        return MetadataRetrievalResult(query, tuple(results), assemble_metadata_cards(results), status)

    async def aretrieve(self, query: MetadataQuery) -> MetadataRetrievalResult:
        # to_thread propagates runtime budget context, and avoids blocking the workflow loop.
        return await asyncio.to_thread(self.retrieve, query)

    def close(self):
        if self._owns_providers and not self._closed:
            for provider in self.providers:
                provider.close()
        self._closed = True

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.close()


def build_metadata_retrieval_service(
    sources: Mapping[str, Mapping[str, Any]] | None = None,
) -> MetadataRetrievalService:
    """Use the existing source registry, separate from topical database_search."""
    from .database_search import build_source_registry

    config = sources if sources is not None else {
        name: {"enabled": True} for name in ("doi_org", "crossref", "datacite")
    }
    registry = build_source_registry()
    tools = []
    try:
        for name, options in config.items():
            if not options.get("enabled", False):
                continue
            source = registry.build(name, options)
            if source.metadata_retrieval_tool is None:
                raise ValueError(f"{name} has no metadata retrieval capability")
            tools.append(source.metadata_retrieval_tool)
        return MetadataRetrievalService(tools, owns_providers=True)
    except Exception:
        for tool in tools:
            tool.close()
        raise
