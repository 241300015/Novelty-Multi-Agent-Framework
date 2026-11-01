"""Offline tests for source-specific DOI providers and their common port."""

from dataclasses import asdict
import json
import socket

import httpx
import pytest

from novelty_agent_framework.core.runtime_artifacts import (
    ProviderPhysicalBudgetExceeded, RuntimeArtifactManager, RuntimeDebugConfig,
)
from novelty_agent_framework.ports import DoiLookupResult, DoiLookupTool
from novelty_agent_framework.tools.database_search import (
    RetrievalSource, StructuredSourceRetrievalTool, build_retrieval_source, build_source_registry,
)
from novelty_agent_framework.tools.database_search.providers.common import (
    HttpRequestPolicy, ProviderConfigurationError, ProviderRequestError,
)
from novelty_agent_framework.tools.database_search.providers.crossref import CrossrefMetadataTool
from novelty_agent_framework.tools.database_search.providers.datacite import DataCiteMetadataTool
from novelty_agent_framework.tools.database_search.providers.doi_org import DoiOrgLookupTool
from novelty_agent_framework.tools.database_search.providers.doi_common import DoiResponseError, normalize_doi


DOI = "10.1038/nature14539"
TOOLS = (DoiOrgLookupTool, CrossrefMetadataTool, DataCiteMetadataTool)
NO_RETRY = HttpRequestPolicy(min_interval_seconds=0, max_retries=0)


@pytest.fixture(autouse=True)
def no_live_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("offline provider tests must not resolve real hosts")
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)


def success(tool_type, doi=DOI):
    if tool_type is DoiOrgLookupTool:
        return {"responseCode": 1, "handle": doi, "values": [
            {"type": "URL", "data": {"format": "string", "value": "https://example.org/paper"}},
        ]}
    if tool_type is CrossrefMetadataTool:
        return {"status": "ok", "message-type": "work", "message": {
            "DOI": doi, "title": ["Deep <i>learning</i>"], "type": "journal-article",
            "author": [{"given": "Yann", "family": "LeCun"}, {"name": "Research Group"}],
            "published": {"date-parts": [[2015, 5, 28]]}, "publisher": "Nature",
            "URL": "https://doi.org/" + doi, "abstract": "<jats:p>Abstract text.</jats:p>",
        }}
    return {"data": {"id": doi, "type": "dois", "attributes": {
        "doi": doi, "state": "findable", "titles": [{"title": "Metadata <b>Schema</b>"}],
        "creators": [{"name": "DataCite Working Group"}, {"givenName": "A", "familyName": "Person"}],
        "publicationYear": 2026, "publisher": {"name": "DataCite"},
        "types": {"resourceTypeGeneral": "Text", "resourceType": "Documentation"},
        "descriptions": [{"descriptionType": "Abstract", "description": "<p>Schema guide.</p>"}],
        "url": "https://example.org/documentation",
    }}}


def missing(tool_type, doi=DOI):
    if tool_type is DoiOrgLookupTool:
        return httpx.Response(404, json={"responseCode": 100, "handle": doi})
    if tool_type is CrossrefMetadataTool:
        return httpx.Response(404, text="Resource not found.")
    return httpx.Response(404, json={"errors": [{"status": "404", "title": "The resource you are looking for doesn't exist."}]})


def tool_with_handler(tool_type, handler):
    return tool_type(client=httpx.Client(transport=httpx.MockTransport(handler)), policy=NO_RETRY)


@pytest.mark.parametrize("tool_type", TOOLS)
def test_exact_lookup_endpoint_and_common_contract(tool_type):
    calls = []

    def handler(request):
        calls.append(request)
        assert request.url.host == httpx.URL(tool_type.base_url).host
        assert request.url.raw_path == (tool_type.path_prefix + "10.1038%2Fnature14539").encode()
        assert request.url.query == b""
        assert "Authorization" not in request.headers
        assert request.headers["Accept"] == "application/json"
        return httpx.Response(200, json=success(tool_type, DOI.upper()))

    tool: DoiLookupTool = tool_with_handler(tool_type, handler)
    result = tool.lookup("https://doi.org/10.1038/NATURE14539")
    assert isinstance(result, DoiLookupResult)
    assert result.source_id == tool.source_id and result.requested_doi == DOI
    assert result.status == "found" and result.http_status == 200
    assert result.record.doi == DOI and result.record.source_id == tool.source_id
    assert len(calls) == 1
    assert json.loads(json.dumps(asdict(result)))["record"]["doi"] == DOI


def test_handle_record_has_no_invented_bibliographic_fields():
    tool = tool_with_handler(DoiOrgLookupTool, lambda _: httpx.Response(200, json=success(DoiOrgLookupTool)))
    result = tool.lookup(DOI)
    assert result.coverage == "handle_registry"
    assert result.record.record_kind == "handle"
    assert result.record.title is None and result.record.authors == () and result.record.year is None
    assert result.record.url == "https://example.org/paper"
    assert not hasattr(tool, "resolve")


def test_crossref_bibliographic_mapping_and_existing_metadata_port():
    tool = tool_with_handler(CrossrefMetadataTool, lambda _: httpx.Response(200, json=success(CrossrefMetadataTool)))
    record = tool.lookup(DOI).record
    assert record.title == "Deep learning"
    assert record.authors == ("Yann LeCun", "Research Group")
    assert record.year == 2015 and record.publisher == "Nature"
    assert record.resource_type == "journal-article" and record.abstract == "Abstract text."
    resolved = tool.resolve("doi:" + DOI)
    assert resolved.title == record.title and resolved.doi == DOI
    assert resolved.quote is None and resolved.location is None


@pytest.mark.parametrize("publisher", ["DataCite", {"name": "DataCite", "publisherIdentifier": "https://ror.org/example"}])
def test_datacite_resource_mapping_preserves_coverage(publisher):
    payload = success(DataCiteMetadataTool)
    payload["data"]["attributes"]["publisher"] = publisher
    tool = tool_with_handler(DataCiteMetadataTool, lambda _: httpx.Response(200, json=payload))
    result = tool.lookup(DOI)
    record = result.record
    assert result.coverage == "datacite_findable"
    assert record.title == "Metadata Schema" and record.publisher == "DataCite"
    assert record.authors == ("DataCite Working Group", "A Person")
    assert record.year == 2026 and record.resource_type == "Text"
    assert record.abstract == "Schema guide."
    assert record.raw_metadata["data"]["attributes"]["types"]["resourceType"] == "Documentation"
    assert tool.resolve(DOI).title == record.title


@pytest.mark.parametrize("tool_type", TOOLS)
def test_explicit_provider_miss(tool_type):
    tool = tool_with_handler(tool_type, lambda _: missing(tool_type))
    result = tool.lookup(DOI)
    assert result.status == "not_found" and result.record is None and result.http_status == 404
    if tool_type is not DoiOrgLookupTool:
        assert tool.resolve(DOI) is None


@pytest.mark.parametrize("tool_type", TOOLS)
@pytest.mark.parametrize("status", [301, 401, 403, 429, 500, 503])
def test_http_errors_not_misses_and_no_default_retries(tool_type, status):
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(status, headers={"Location": "https://unexpected.example/"}, json={"errors": []})
    tool = tool_with_handler(tool_type, handler)
    with pytest.raises(ProviderRequestError) as caught:
        tool.lookup(DOI)
    assert caught.value.status_code == status and len(calls) == 1


@pytest.mark.parametrize("tool_type", TOOLS)
@pytest.mark.parametrize("body,content_type", [
    (b"<html>captcha</html>", "text/html"), (b"{broken", "application/json"),
    (b"[]", "application/json"), (b"{}", "application/json"),
])
def test_invalid_success_payload_is_not_a_miss(tool_type, body, content_type):
    tool = tool_with_handler(tool_type, lambda _: httpx.Response(200, content=body, headers={"Content-Type": content_type}))
    with pytest.raises(DoiResponseError):
        tool.lookup(DOI)


@pytest.mark.parametrize("tool_type", TOOLS)
def test_html_404_is_not_a_valid_negative(tool_type):
    tool = tool_with_handler(tool_type, lambda _: httpx.Response(404, text="<html>edge not found</html>"))
    with pytest.raises((ProviderRequestError, DoiResponseError)):
        tool.lookup(DOI)


@pytest.mark.parametrize("tool_type", TOOLS)
def test_wrong_doi_in_success_is_rejected(tool_type):
    tool = tool_with_handler(tool_type, lambda _: httpx.Response(200, json=success(tool_type, "10.9999/unrelated")))
    with pytest.raises(DoiResponseError, match="does not match"):
        tool.lookup(DOI)


@pytest.mark.parametrize("payload", [
    {"responseCode": True, "handle": DOI, "values": []},
    {"responseCode": 100, "handle": DOI},
    {"responseCode": 2, "handle": DOI},
    {"responseCode": 1, "handle": DOI},
])
def test_handle_codes_do_not_fabricate_registration(payload):
    tool = tool_with_handler(DoiOrgLookupTool, lambda _: httpx.Response(200, json=payload))
    with pytest.raises(DoiResponseError):
        tool.lookup(DOI)


def test_handle_values_not_found_still_means_existing_handle():
    tool = tool_with_handler(DoiOrgLookupTool, lambda _: httpx.Response(200, json={"responseCode": 200, "handle": DOI}))
    result = tool.lookup(DOI)
    assert result.status == "found" and result.record.url is None and result.record.title is None


@pytest.mark.parametrize("field", ["id", "doi"])
def test_datacite_both_identity_fields_checked(field):
    payload = success(DataCiteMetadataTool)
    if field == "id":
        payload["data"]["id"] = "10.9999/other"
    else:
        payload["data"]["attributes"]["doi"] = "10.9999/other"
    tool = tool_with_handler(DataCiteMetadataTool, lambda _: httpx.Response(200, json=payload))
    with pytest.raises(DoiResponseError, match="does not match"):
        tool.lookup(DOI)


@pytest.mark.parametrize("title", [None, [], "API endpoint not found"])
def test_datacite_unrecognized_404_preserves_failure(title):
    tool = tool_with_handler(DataCiteMetadataTool, lambda _: httpx.Response(404, json={"errors": [{"status": "404", "title": title}]}))
    with pytest.raises(ProviderRequestError):
        tool.lookup(DOI)


@pytest.mark.parametrize("tool_type", [CrossrefMetadataTool, DataCiteMetadataTool])
def test_missing_title_is_partial_metadata_not_a_missing_record(tool_type):
    payload = success(tool_type)
    if tool_type is CrossrefMetadataTool:
        payload["message"]["title"] = []
    else:
        payload["data"]["attributes"]["titles"] = []
    tool = tool_with_handler(tool_type, lambda _: httpx.Response(200, json=payload))
    assert tool.lookup(DOI).status == "found" and tool.lookup(DOI).record.title is None
    with pytest.raises(DoiResponseError, match="no title"):
        tool.resolve(DOI)


@pytest.mark.parametrize("value,expected", [
    (DOI.upper(), DOI), (" doi: " + DOI + " ", DOI),
    ("https://doi.org/10.1038%2Fnature14539", DOI),
    ("http://dx.doi.org/" + DOI, DOI),
    ("10.1234/suffix.(1).", "10.1234/suffix.(1)."),
    ("10.1234/a%2Fb", "10.1234/a%2fb"),
])
def test_doi_normalization(value, expected):
    assert normalize_doi(value) == expected


@pytest.mark.parametrize("value", ["", "nature14539", "10.1234/", "10.1234/a b", "10.1234/a\nb", "https://evil.example/" + DOI, "https://doi.org/" + DOI + "?x=1", "https://doi.org@evil.example/" + DOI, None])
def test_invalid_input_never_calls_network(value):
    def handler(_):
        pytest.fail("invalid DOI should not send a request")
    tool = tool_with_handler(CrossrefMetadataTool, handler)
    with pytest.raises(ValueError):
        tool.lookup(value)


def test_suffix_delimiters_are_encoded_once_without_path_traversal():
    doi = "10.1234/a/../b?x=1#fragment%value"
    def handler(request):
        assert request.url.raw_path == b"/works/10.1234%2Fa%2F..%2Fb%3Fx%3D1%23fragment%25value"
        assert request.url.query == b"" and request.url.fragment == ""
        return httpx.Response(200, json=success(CrossrefMetadataTool, doi))
    assert tool_with_handler(CrossrefMetadataTool, handler).lookup(doi).record.doi == doi


@pytest.mark.parametrize("error", [httpx.ConnectError, httpx.ReadTimeout])
def test_transport_failure_not_cached_and_sanitized(error):
    calls = []
    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            raise error("sensitive-proxy-address", request=request)
        return httpx.Response(200, json=success(CrossrefMetadataTool))
    tool = tool_with_handler(CrossrefMetadataTool, handler)
    with pytest.raises(ProviderRequestError) as caught:
        tool.lookup(DOI)
    assert caught.value.transport_error_type == error.__name__
    assert "sensitive" not in str(caught.value)
    assert tool.lookup(DOI).status == "found" and len(calls) == 2


def test_caller_client_is_not_closed_and_redirects_are_not_followed():
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(302, headers={"Location": "https://other.example/path"})
    with httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True) as client:
        with CrossrefMetadataTool(client=client, policy=NO_RETRY) as tool:
            with pytest.raises(ProviderRequestError):
                tool.lookup(DOI)
        assert not client.is_closed and len(calls) == 1
        with pytest.raises(RuntimeError, match="closed"):
            tool.lookup(DOI)


def test_default_transport_uses_ipv4_tls_and_ignores_proxy_env(monkeypatch):
    original_transport = httpx.HTTPTransport
    settings = []
    def capture(**kwargs):
        settings.append(kwargs)
        return original_transport(**kwargs)
    monkeypatch.setattr(httpx, "HTTPTransport", capture)
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1")
    with DoiOrgLookupTool() as tool:
        assert tool.transport.policy.max_retries == 0
        assert not tool.transport.client.is_closed
    assert settings == [{"local_address": "0.0.0.0", "trust_env": False, "retries": 0}]
    assert tool.transport.client.is_closed


@pytest.mark.parametrize("settings", [
    {"timeout": 0}, {"timeout": float("nan")}, {"ip_family": "invalid"},
    {"base_url": "http://doi.org"}, {"base_url": "https://user:secret@doi.org"},
    {"base_url": "https://doi.org/path"}, {"user_agent": "agent\r\nInjected: 1"},
])
def test_bad_configuration_fails_before_client_creation(settings):
    with pytest.raises(ProviderConfigurationError):
        DoiOrgLookupTool(**settings)


@pytest.mark.parametrize("source_id", ["doi_org", "crossref", "datacite"])
def test_same_registry_builds_metadata_only_capability_bundle(source_id, tmp_path):
    source = build_retrieval_source({"sources": {source_id: {"enabled": True}}}, source_id=source_id)
    try:
        assert source.doi_lookup_tool.source_id == source_id
        assert source.query_adapter is None and source.search_tool is None and source.full_text_tool is None
        assert (source.metadata_tool is not None) == (source_id != "doi_org")
        if source_id != "doi_org":
            assert source.metadata_tool is source.doi_lookup_tool
        with pytest.raises(ValueError, match="query_adapter is required"):
            StructuredSourceRetrievalTool(source=source)
    finally:
        source.doi_lookup_tool.close()
    disabled = build_source_registry().build(source_id, {"enabled": False})
    assert disabled.doi_lookup_tool is None and disabled.metadata_tool is None


def test_source_rejects_mixed_lookup_provider():
    with CrossrefMetadataTool() as tool:
        with pytest.raises(ValueError, match="doi_lookup_tool"):
            RetrievalSource(source_id="datacite", doi_lookup_tool=tool)


def test_runtime_budget_counts_doi_provider_requests(tmp_path):
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(200, json=success(CrossrefMetadataTool))
    tool = tool_with_handler(CrossrefMetadataTool, handler)
    manager = RuntimeArtifactManager("paper", config=RuntimeDebugConfig(
        output_root=tmp_path / "output", archive_root=tmp_path / "archive", max_physical_provider_requests=1))
    manager.activate()
    try:
        assert tool.lookup(DOI).status == "found"
        with pytest.raises(ProviderPhysicalBudgetExceeded):
            tool.lookup(DOI)
    finally:
        manager.deactivate()
    assert len(calls) == 1
    assert tool.transport.provider == "crossref"


@pytest.mark.parametrize("source_id", ["doi_org", "crossref", "datacite"])
def test_lookup_source_cannot_claim_query_adapter(source_id):
    with pytest.raises(ProviderConfigurationError, match="query adapter"):
        build_source_registry().build(source_id, {"enabled": True, "adapter_only": True})


def test_negative_handle_identity_is_verified():
    tool = tool_with_handler(DoiOrgLookupTool, lambda _: missing(DoiOrgLookupTool, "10.9999/wrong"))
    with pytest.raises(DoiResponseError, match="does not match"):
        tool.lookup(DOI)


def test_datacite_conflicting_404_is_not_a_valid_negative():
    payload = missing(DataCiteMetadataTool).json()
    payload.update(success(DataCiteMetadataTool))
    tool = tool_with_handler(DataCiteMetadataTool, lambda _: httpx.Response(404, json=payload))
    with pytest.raises(ProviderRequestError):
        tool.lookup(DOI)
