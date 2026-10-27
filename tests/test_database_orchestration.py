"""Database fan-out is owned by the tool, not by model choices."""
import asyncio
from types import SimpleNamespace

import pytest

from novelty_agent_framework.schemas import DatabaseSearchArguments, ResearchBundle
from novelty_agent_framework.tools.database_search import DatabaseSearchTool
from novelty_agent_framework.tools.database_search.structured_retrieval import StructuredSourceRetrievalTool
from novelty_agent_framework.tools.database_search.retrieval_sources import RetrievalSource
from novelty_agent_framework.tools import ResearcherToolRegistry
from novelty_agent_framework.core.tool_call_harness import _database_result_complete
from novelty_agent_framework.workflows.research_reading_policy import ResearchReadingPolicy
from test_database_search_tool import build_tool, scope, SecondQueryAdapter, SecondSearcher, SecondFullTexts


def databases(tmp_path):
    first, store, _ = build_tool(tmp_path)
    second = StructuredSourceRetrievalTool(source=RetrievalSource(source_id="demo2",
        query_adapter=SecondQueryAdapter(), search_tool=SecondSearcher(), full_text_tool=SecondFullTexts()),
        reference_store=store, candidate_limit=2, full_text_limit=0)
    first.tools_by_source["demo"].full_text_limit = 0
    return DatabaseSearchTool({**first.tools_by_source, "demo2": second}, store)


def test_one_discovery_calls_every_configured_database_and_merges_trusted_bundle(tmp_path):
    tool = databases(tmp_path)
    result = asyncio.run(tool.ainvoke(DatabaseSearchArguments(), scope=scope()))
    assert result.succeeded
    assert {s["source_id"] for s in result.payload["source_statuses"]} == {"demo", "demo2"}
    bundle = ResearchBundle.model_validate(result.payload["research_bundle"])
    assert {s.source_id for s in bundle.search_executions} == {"demo", "demo2"}
    assert {s.source_id for s in bundle.source_records} == {"demo", "demo2"}
    assert _database_result_complete(result)
    policy = ResearchReadingPolicy(ResearcherToolRegistry([tool]), scope())
    assert policy.choices() == {} and not policy.can_finish
    call = policy.initial_tool_call()
    assert call.arguments == {}
    assert policy.initial_tool_call() is None


def test_one_database_exception_keeps_other_database_results_and_failure_identity(tmp_path):
    tool = databases(tmp_path)
    async def fail(request):
        raise TimeoutError("injected")
    tool.tools_by_source["demo"].ainvoke = fail
    result = asyncio.run(tool.ainvoke(DatabaseSearchArguments(), scope=scope()))
    assert result.succeeded and result.payload["execution_summary"]["degraded"]
    assert not _database_result_complete(result)
    assert [s["succeeded"] for s in result.payload["source_statuses"]] == [False, True]
    assert result.payload["execution_issues"][0]["scope"]["provider"] == "demo"
    assert result.payload["database_search_result"]["results"]
    ResearchBundle.model_validate(result.payload["research_bundle"])


def test_recovery_source_is_injected_in_scope_not_selected_by_model(tmp_path):
    tool = databases(tmp_path)
    request = scope().model_copy(update={"recovery": SimpleNamespace(source_id="demo2")})
    result = asyncio.run(tool.ainvoke(DatabaseSearchArguments(), scope=request))
    assert [s["source_id"] for s in result.payload["source_statuses"]] == ["demo2"]


def test_cancellation_propagates_without_starting_other_database(tmp_path):
    tool = databases(tmp_path)
    async def cancel(request):
        raise asyncio.CancelledError()
    tool.tools_by_source["demo"].ainvoke = cancel
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(tool.ainvoke(DatabaseSearchArguments(), scope=scope()))
