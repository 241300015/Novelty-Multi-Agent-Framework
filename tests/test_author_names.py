from novelty_agent_framework.core.author_names import author_name_versions, author_names_key
from novelty_agent_framework.ports import MetadataQuery
from novelty_agent_framework.tools.metadata_retrieval import MetadataRetrievalService
from novelty_agent_framework.tools.database_search.providers.crossref import CrossrefMetadataTool
from novelty_agent_framework.tools.database_search.providers.datacite import DataCiteMetadataTool
from test_metadata_retrieval import provider, record, search_payload, tool_with_handler
import httpx


def test_versions_and_ordered_comparison():
    assert author_name_versions(" Lee, Charlotte E. ") == ("Charlotte E. Lee", "Lee, Charlotte E.")
    assert author_name_versions("Charlotte E. Lee")[1] == "Lee, Charlotte E."
    assert author_name_versions("Lee") == ("Lee",)
    assert author_names_key(["Lee, Charlotte E.", "Alice Smith"]) == author_names_key(["Charlotte E. Lee", "Smith, Alice"])
    assert author_names_key(["Lee, Charlotte E."]) != author_names_key(["Charlotte F. Lee"])
    assert author_names_key(["Lee, Charlotte E."]) != author_names_key(["E. Lee, Charlotte"])
    assert author_names_key(["Lee, Charlotte E."]) != author_names_key(["Lee, Charlotte E., Jr."])
    assert author_names_key(["de la Cruz, Ana"]) != author_names_key(["Ana de la Cruz"])
    assert author_names_key(["Lee"]) == author_names_key(["lee"])
    assert author_names_key(["Alice Smith", "Bob Jones"]) != author_names_key(["Bob Jones", "Alice Smith"])


def test_sources_with_different_name_order_merge_without_conflict():
    query = MetadataQuery(title="Work")
    result = MetadataRetrievalService([
        provider("crossref", [record(title="Work", authors=("Charlotte E. Lee",))]),
        provider("datacite", [record("datacite", title="Work", authors=("Lee, Charlotte E.",))]),
    ]).retrieve(query)
    assert len(result.cards) == 1
    assert result.cards[0].field_sources["authors"] == ("crossref:0", "datacite:0")
    assert "authors" not in result.cards[0].conflicts


def test_sources_with_different_explicit_boundaries_remain_separate():
    result = MetadataRetrievalService([
        provider("crossref", [record(title="Work", authors=("Lee, Charlotte E.",))]),
        provider("datacite", [record("datacite", title="Work", authors=("E. Lee, Charlotte",))]),
    ]).retrieve(MetadataQuery(title="Work"))
    assert len(result.cards) == 2
    assert all("authors" in card.conflicts for card in result.cards)


def test_provider_search_uses_both_formats():
    query = MetadataQuery(title="Work", authors=("Lee, Charlotte E.", "Alice Smith"), year=2020)
    for tool_type in (CrossrefMetadataTool, DataCiteMetadataTool):
        def handler(request):
            params = request.url.params
            if tool_type is CrossrefMetadataTool:
                assert params["query.author"] == "Charlotte E. Lee Alice Smith"
            else:
                assert '(creators.name:"Charlotte E. Lee" OR creators.name:"Lee, Charlotte E.")' in params["query"]
                assert '(creators.name:"Alice Smith" OR creators.name:"Smith, Alice")' in params["query"]
            return httpx.Response(200, json=search_payload(tool_type))
        assert tool_with_handler(tool_type, handler).retrieve(query).status == "found"
