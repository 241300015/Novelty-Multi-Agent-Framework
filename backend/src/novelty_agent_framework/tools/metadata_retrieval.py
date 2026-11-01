"""Retrieve independent source facts and assemble metadata cards for Validator."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from dataclasses import replace
from typing import Any

from ..core.author_names import author_names_key
from ..ports import (
    DoiMetadataRecord, MetadataCard, MetadataFieldValue, MetadataQuery,
    MetadataRetrievalResult, MetadataRetrievalTool, ProviderMetadataResult,
)
from .database_search.providers.doi_common import metadata_failure_code, normalize_doi

_FIELDS = ("doi", "title", "authors", "year", "url", "publisher", "venue", "volume", "issue", "resource_type", "abstract", "pages", "page_count")


def _value_key(value):
    if isinstance(value, str):
        return " ".join(value.split()).casefold()
    if isinstance(value, tuple):
        return tuple(_value_key(item) for item in value)
    return value


def _field_key(name, value):
    return author_names_key(value) if name == "authors" else _value_key(value)


def assemble_metadata_cards(results: Sequence[ProviderMetadataResult]) -> tuple[MetadataCard, ...]:
    """Build compatible candidates, keeping conflicting same-DOI records apart."""
    records = []
    for result in results:
        for index, record in enumerate(result.records):
            doi = normalize_doi(record.doi) if record.doi else None
            records.append((f"{result.source_id}:{index}", replace(record, doi=doi)))

    def populated(value):
        return value is not None and value != () and value != ""

    identity_fields = ("title", "authors", "year")
    # URLs are alternative locations. Different observed metadata values stay separate.
    conflict_fields = ("title", "authors", "year", "publisher", "venue", "volume", "issue", "resource_type", "abstract", "pages", "page_count")
    groups = []
    for ref, record in records:
        def compatible(group):
            for _, other in group:
                if record.doi and other.doi:
                    if record.doi != other.doi:
                        return False
                else:
                    if not all(populated(getattr(record, field)) and populated(getattr(other, field))
                               and _field_key(field, getattr(record, field)) == _field_key(field, getattr(other, field))
                               for field in identity_fields):
                        return False
                if any(populated(getattr(record, field)) and populated(getattr(other, field))
                       and _field_key(field, getattr(record, field)) != _field_key(field, getattr(other, field))
                       for field in conflict_fields):
                    return False
            return True
        group = next((group for group in groups if compatible(group)), None)
        if group is None:
            groups.append([(ref, record)])
        else:
            group.append((ref, record))

    def related(left, right):
        if left.doi and right.doi:
            return left.doi == right.doi
        return all(populated(getattr(left, field)) and populated(getattr(right, field))
                   and _field_key(field, getattr(left, field)) == _field_key(field, getattr(right, field))
                   for field in identity_fields)

    def related_conflicts(group):
        known_dois = {member.doi for _, member in group if member.doi}
        comparable = [(ref, record) for ref, record in records
                      if (not record.doi or not known_dois or record.doi in known_dois)
                      and any(related(record, member) for _, member in group)]
        conflicts = {}
        for name in conflict_fields:
            observed = {}
            for ref, record in comparable:
                value = getattr(record, name)
                if populated(value):
                    observed.setdefault(_field_key(name, value), [value, []])[1].append(ref)
            if len(observed) > 1:
                conflicts[name] = tuple(MetadataFieldValue(value, tuple(refs)) for value, refs in observed.values())
        return conflicts

    cards = []
    for group in groups:
        refs = dict(group)
        values, field_sources = {}, {}
        urls = tuple(dict.fromkeys(r.url for _, r in group if r.url))
        for name in _FIELDS:
            observed = {}
            for ref, record in group:
                value = getattr(record, name)
                if populated(value):
                    observed.setdefault(_field_key(name, value), [value, []])[1].append(ref)
            if len(observed) == 1:
                value, sources = next(iter(observed.values()))
                values[name], field_sources[name] = value, tuple(sources)
        kind = "bibliographic" if any(r.record_kind == "bibliographic" for _, r in group) else "handle"
        metadata = DoiMetadataRecord(source_id="merged", doi=values.pop("doi", None), record_kind=kind, **values)
        missing = tuple(name for name in _FIELDS if not populated(getattr(metadata, name)))
        candidate_id = "candidate:" + "+".join(ref for ref, _ in group)
        cards.append(MetadataCard(candidate_id, metadata, field_sources,
                                  related_conflicts(group), refs, urls, missing))
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
            first_error = None
            for provider in self.providers:
                try:
                    provider.close()
                except Exception as exc:
                    if first_error is None:
                        first_error = exc
            self._closed = True
            if first_error is not None:
                raise first_error
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
            try:
                tool.close()
            except Exception:
                pass  # Preserve the startup exception while closing all created clients.
        raise
