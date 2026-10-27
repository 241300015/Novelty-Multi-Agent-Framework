"""Offline contracts, search boundaries and cross-provider assembly."""
from dataclasses import asdict
import asyncio
import json
import socket
from types import SimpleNamespace

import httpx
import pytest

from novelty_agent_framework.ports import DoiMetadataRecord, MetadataQuery, ProviderMetadataResult
from novelty_agent_framework.tools.metadata_retrieval import (
    MetadataRetrievalService, build_metadata_retrieval_service,
)
from novelty_agent_framework.tools.database_search.providers.crossref import CrossrefMetadataTool
from novelty_agent_framework.tools.database_search.providers.datacite import DataCiteMetadataTool
from novelty_agent_framework.tools.database_search.providers.doi_org import DoiOrgLookupTool
from test_doi_metadata_providers import DOI, TOOLS, success, tool_with_handler


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("no live network in metadata contract tests")
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)


def record(source="crossref", **changes):
    return DoiMetadataRecord(**dict(dict(source_id=source, doi=DOI,
        record_kind="bibliographic"), **changes))


def provider(source, records=(), *, status=None, callback=None):
    def retrieve(query):
        if callback:
            callback(query)
        return ProviderMetadataResult(source, query, status or ("found" if records else "not_found"),
                                      source, tuple(records))
    return SimpleNamespace(source_id=source, retrieve=retrieve)


def search_payload(tool_type, items=None, total=None):
    if items is None:
        items = [success(tool_type)["message" if tool_type is CrossrefMetadataTool else "data"]]
    if total is None:
        total = len(items)
    if tool_type is CrossrefMetadataTool:
        return {"status": "ok", "message-type": "work-list", "message": {
            "items": items, "total-results": total}}
    return {"data": items, "meta": {"total": total}}


@pytest.mark.parametrize("tool_type", TOOLS)
def test_uniform_exact_lookup(tool_type):
    query = MetadataQuery(doi=DOI)
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(200, json=success(tool_type))
    result = tool_with_handler(tool_type, handler).retrieve(query)
    assert result.query == query and result.status == "found"
    assert result.records[0].doi == DOI and len(calls) == 1


def test_handle_title_search_explicitly_unsupported():
    def forbidden(request):
        pytest.fail("Handle registry must not pretend to support title search")
    result = tool_with_handler(DoiOrgLookupTool, forbidden).retrieve(MetadataQuery(title="Deep learning"))
    assert result.status == "unsupported" and result.records == () and result.http_status is None


@pytest.mark.parametrize("tool_type", [CrossrefMetadataTool, DataCiteMetadataTool])
def test_bibliographic_search_params_and_candidates(tool_type):
    query = MetadataQuery(title='A "quoted" title', authors=("A Person",), year=2026, limit=2)
    calls = []
    def handler(request):
        calls.append(request)
        params = request.url.params
        assert request.url.path == ("/works" if tool_type is CrossrefMetadataTool else "/dois")
        if tool_type is CrossrefMetadataTool:
            assert params["rows"] == "2"
            assert params["query.bibliographic"] == 'A "quoted" title'
            assert params["filter"] == "from-pub-date:2026-01-01,until-pub-date:2026-12-31"
            assert params["query.author"] == "A Person"
        else:
            assert params["page[size]"] == "2"
            assert params["query"] == 'titles.title:"A \\"quoted\\" title" AND creators.name:"A Person" AND publicationYear:2026'
        return httpx.Response(200, json=search_payload(tool_type, total=20))
    result = tool_with_handler(tool_type, handler).retrieve(query)
    assert result.status == "found" and result.truncated and len(calls) == 1
    assert result.records[0].doi == DOI
    assert result.records[0].title != query.title  # queries are not source facts


@pytest.mark.parametrize("tool_type", [CrossrefMetadataTool, DataCiteMetadataTool])
def test_missing_search_fields_remain_empty(tool_type):
    item = {} if tool_type is CrossrefMetadataTool else {"type": "dois", "attributes": {}}
    tool = tool_with_handler(tool_type, lambda _: httpx.Response(200, json=search_payload(tool_type, [item])))
    result = tool.retrieve(MetadataQuery(title="Claimed title", year=2026))
    assert result.status == "found"
    actual = result.records[0]
    assert actual.doi is None and actual.title is None and actual.year is None
    assert actual.authors == () and actual.url is None


@pytest.mark.parametrize("tool_type", [CrossrefMetadataTool, DataCiteMetadataTool])
@pytest.mark.parametrize("mode", ["empty", "unavailable", "invalid_json", "malformed", "wrong_endpoint"])
def test_search_miss_is_distinct_from_failure(tool_type, mode):
    responses = {
        "empty": httpx.Response(200, json=search_payload(tool_type, [])),
        "unavailable": httpx.Response(503, text="unavailable"),
        "invalid_json": httpx.Response(200, text="not json"),
        "malformed": httpx.Response(200, json={}),
        "wrong_endpoint": httpx.Response(404, text="not found"),
    }
    result = tool_with_handler(tool_type, lambda _: responses[mode]).retrieve(MetadataQuery(title="Title"))
    assert result.status == ("not_found" if mode == "empty" else "error")
    assert result.records == ()
    assert bool(result.error_type) == (mode != "empty")


def test_merge_fills_only_same_identity_and_tracks_every_field():
    service = MetadataRetrievalService([
        provider("doi_org", [record("doi_org", record_kind="handle", url="https://publisher.test/work")]),
        provider("crossref", [record(title="Work", authors=("Alice",), year=2026)]),
        provider("datacite", [record("datacite", title=" work ", publisher="Publisher", abstract="Abstract")]),
    ])
    result = service.retrieve(MetadataQuery(doi=DOI))
    card = result.card
    assert len(result.providers) == 3 and result.status == "complete"
    assert card.metadata.title == "Work" and card.metadata.year == 2026
    assert card.metadata.publisher == "Publisher" and card.metadata.url == "https://publisher.test/work"
    assert card.field_sources["title"] == ("crossref:0", "datacite:0")
    assert card.field_sources["year"] == ("crossref:0",)
    assert card.metadata.venue is None and "venue" in card.missing_fields
    assert len(card.records) == 3 and card.conflicts == {}
    assert json.loads(json.dumps(asdict(result)))["cards"][0]["metadata"]["title"] == "Work"


def test_conflicting_facts_are_not_silently_selected():
    result = MetadataRetrievalService([
        provider("crossref", [record(title="First", year=2025, url="https://a.test")]),
        provider("datacite", [record("datacite", title="Second", year=2026, url="https://b.test")]),
    ]).retrieve(MetadataQuery(doi=DOI))
    card = result.card
    assert card.metadata.title is None and card.metadata.year is None and card.metadata.url is None
    assert set(card.conflicts) == {"title", "year"}
    assert card.conflicts["year"][0].record_refs == ("crossref:0",)
    assert card.urls == ("https://a.test", "https://b.test")


def test_same_title_with_different_or_missing_doi_remains_separate():
    result = MetadataRetrievalService([
        provider("crossref", [record(title="Same"), record(doi=None, title="Same")]),
        provider("datacite", [record("datacite", doi="10.1234/other", title="Same"),
                              record("datacite", doi=None, title="Same")]),
    ]).retrieve(MetadataQuery(title="Same"))
    assert result.card is None and len(result.cards) == 4


def test_failure_does_not_erase_other_results_or_leak_exception_details():
    calls = []
    def failure(query):
        calls.append("failed")
        raise RuntimeError("secret-token-do-not-serialize")
    result = MetadataRetrievalService([
        provider("broken", callback=failure), provider("crossref", [record(title="Work")]),
    ]).retrieve(MetadataQuery(doi=DOI))
    assert result.status == "partial" and result.card.metadata.title == "Work"
    assert result.providers[0].status == "error" and calls == ["failed"]
    assert "secret-token" not in json.dumps(asdict(result))


@pytest.mark.parametrize("status, expected", [("error", "failed"), ("unsupported", "unsupported"),
                                               ("not_found", "complete")])
def test_empty_aggregate_distinguishes_execution_status(status, expected):
    result = MetadataRetrievalService([provider("test", status=status)]).retrieve(MetadataQuery(title="Work"))
    assert result.status == expected and result.card is None and result.cards == ()


def test_wrong_exact_identity_is_provider_error():
    result = MetadataRetrievalService([provider("crossref", [record(doi="10.1234/other")])]).retrieve(
        MetadataQuery(doi=DOI))
    assert result.status == "failed" and result.providers[0].error_type == "ValueError"
    assert result.cards == ()


def test_registry_builds_all_three_and_closes_owned_clients():
    with build_metadata_retrieval_service() as service:
        assert [p.source_id for p in service.providers] == ["doi_org", "crossref", "datacite"]
        assert all(not p.transport.client.is_closed for p in service.providers)
    assert all(p.transport.client.is_closed for p in service.providers)


def test_async_service():
    result = asyncio.run(MetadataRetrievalService([provider("crossref", [record(title="Work")])]).aretrieve(
        MetadataQuery(doi=DOI)))
    assert result.card.metadata.title == "Work"


@pytest.mark.parametrize("values", [{}, {"title": " "}, {"title": "T", "limit": 0},
                                    {"title": "T", "year": True}, {"title": "T", "authors": "Alice"}])
def test_invalid_query(values):
    with pytest.raises(ValueError):
        MetadataQuery(**values)
