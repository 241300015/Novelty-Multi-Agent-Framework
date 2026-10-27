from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from novelty_agent_framework.agents import DemoQueryAdapter
from novelty_agent_framework.persistence import ReferenceStore
from novelty_agent_framework.ports import FullText, SearchHit
from novelty_agent_framework.schemas import (
    AccessStatus,
    DatabaseSearchArguments,
    NoveltyPoint,
    ResearchBundle,
    ResearchTask,
    SearchExecution,
    SearchExecutionStatus,
    SourceKind,
    SourceRecord,
    TaskResearchRequest,
    Work,
    WorkType,
)
from novelty_agent_framework.tools.database_search import (
    DatabaseSearchTool,
    RetrievalSource,
    StructuredSourceRetrievalTool,
)
from novelty_agent_framework.tools.database_search.tool import (
    _summarize_search_executions,
)
from conftest import minimal_search_plan


class Planner:
    def __init__(self) -> None:
        self.calls = []

    def plan(self, point, task):
        self.calls.append((point, task))
        raise AssertionError("legacy planner path must not be called")


class Searcher:
    source_id = "demo"

    def search(self, query, *, limit=10):
        return [
            SearchHit(
                document_id="paper-1",
                external_id="paper-1v2",
                source_id="demo",
                title="Structured Candidate",
                abstract="A database abstract with auditable raw metadata.",
                authors=("Alice", "Bob"),
                year=2025,
                url="https://example.test/paper-1",
                raw_metadata={"private_provider_field": "audit-only"},
            )
        ]


class FullTexts:
    source_id = "demo"

    def fetch(self, document_id):
        return FullText(
            document_id=document_id,
            title="Structured Candidate",
            text="Trusted database artifact text.",
            content_extent="full",
            source_url="https://example.test/paper-1.txt",
        )


class SecondSearcher(Searcher):
    source_id = "demo2"


class SecondFullTexts(FullTexts):
    source_id = "demo2"


class SecondQueryAdapter(DemoQueryAdapter):
    database = "demo2"


def execution(status: SearchExecutionStatus, index: int) -> SearchExecution:
    now = datetime.now(timezone.utc)
    return SearchExecution(
        execution_id=f"execution-{index}",
        run_id="run-1",
        tool_name="structured_source_retrieval",
        source_id="demo",
        query=f"query-{index}",
        status=status,
        started_at=now,
        completed_at=now,
        error=(
            "TimeoutError: timed out"
            if status == SearchExecutionStatus.FAILED
            else None
        ),
    )


def bundle(*statuses: SearchExecutionStatus, with_result: bool = False) -> ResearchBundle:
    now = datetime.now(timezone.utc)
    return ResearchBundle(
        bundle_id="bundle-1",
        producer="structured_source_retrieval:demo",
        search_executions=[
            execution(status, index) for index, status in enumerate(statuses)
        ],
        works=(
            [Work(work_id="work-1", work_type=WorkType.ARTICLE, title="Candidate")]
            if with_result
            else []
        ),
        source_records=(
            [
                SourceRecord(
                    source_record_id="record-1",
                    work_id="work-1",
                    source_id="demo",
                    source_kind=SourceKind.STRUCTURED_DATABASE,
                    title="Candidate",
                    access_status=AccessStatus.DISCOVERED,
                    observed_at=now,
                )
            ]
            if with_result
            else []
        ),
    )


class StaticRetrieval:
    source_id = "demo"

    def __init__(self, value: ResearchBundle, store: ReferenceStore):
        self.value = value
        self.reference_store = store

    async def ainvoke(self, _request):
        return self.value


def observe_bundle(tmp_path, value: ResearchBundle):
    store = ReferenceStore(tmp_path)
    tool = DatabaseSearchTool({"demo": StaticRetrieval(value, store)}, store)
    observation = asyncio.run(
        tool.ainvoke(DatabaseSearchArguments(source_id="demo"), scope=scope())
    )
    return tool, observation


def scope() -> TaskResearchRequest:
    return TaskResearchRequest(
        subject_paper_id="subject-1",
        run_id="run-1",
        novelty_point=NoveltyPoint(
            point_id="NP-1", claim="graph novelty", technical_features=["graph"]
        ),
        research_task=ResearchTask(
            task_id="T-1", novelty_point_id="NP-1", task_type="search", language="en"
        ),
        search_plan=minimal_search_plan("T-1", "NP-1"),
    )


def build_tool(tmp_path):
    store = ReferenceStore(tmp_path)
    planner = Planner()
    internal = StructuredSourceRetrievalTool(
        search_planner=planner,
        source=RetrievalSource(
            source_id="demo",
            query_adapter=DemoQueryAdapter(),
            search_tool=Searcher(),
            full_text_tool=FullTexts(),
        ),
        reference_store=store,
        candidate_limit=2,
    )
    return DatabaseSearchTool({"demo": internal}, store), store, planner


def test_arguments_only_accept_and_normalize_source_id():
    assert DatabaseSearchArguments(source_id=" DeMo ").source_id == "demo"
    with pytest.raises(ValidationError):
        DatabaseSearchArguments(source_id="demo", query="untrusted")


def test_description_lists_configured_sources_and_reader_policy(tmp_path):
    tool, _, _ = build_tool(tmp_path)

    assert "已配置数据库：demo" in tool.description
    assert "artifact_ids" in tool.description
    assert "reader" in tool.description


def test_description_whitelists_every_configured_source(tmp_path):
    store = ReferenceStore(tmp_path)
    planner = Planner()
    internal = StructuredSourceRetrievalTool(
        search_planner=planner,
        source=RetrievalSource(
            source_id="demo",
            query_adapter=DemoQueryAdapter(),
            search_tool=Searcher(),
            full_text_tool=FullTexts(),
        ),
        reference_store=store,
        candidate_limit=2,
    )
    internal2 = StructuredSourceRetrievalTool(
        search_planner=planner,
        source=RetrievalSource(
            source_id="demo2",
            query_adapter=SecondQueryAdapter(),
            search_tool=SecondSearcher(),
            full_text_tool=SecondFullTexts(),
        ),
        reference_store=store,
        candidate_limit=2,
    )
    tool = DatabaseSearchTool({"demo": internal, "demo2": internal2}, store)

    assert "已配置数据库：demo, demo2" in tool.description


def test_unknown_source_fails_clearly(tmp_path):
    tool, _, planner = build_tool(tmp_path)
    with pytest.raises(ValueError, match="unavailable"):
        asyncio.run(tool.ainvoke(DatabaseSearchArguments(source_id="other"), scope=scope()))
    assert planner.calls == []


def test_execution_summary_counts_provider_neutral_statuses():
    summary = _summarize_search_executions(
        [
            execution(SearchExecutionStatus.SUCCEEDED, 1),
            execution(SearchExecutionStatus.PARTIAL, 2),
            execution(SearchExecutionStatus.FAILED, 3),
            execution(SearchExecutionStatus.REQUIRES_HUMAN, 4),
        ]
    )

    assert summary == {
        "total": 4,
        "succeeded": 1,
        "partial": 1,
        "failed": 1,
        "not_run": 0,
        "requires_human": 1,
        "degraded": True,
        "all_failed": False,
        "provider_failed": True,
        "no_execution": False,
        "retrieval_incomplete_budget": False,
    }


def test_execution_summary_all_failed_is_structured_failure(tmp_path):
    tool, observation = observe_bundle(
        tmp_path,
        bundle(*([SearchExecutionStatus.FAILED] * 3)),
    )

    assert observation.succeeded is False
    assert observation.error == "all 3 search executions failed"
    assert observation.payload["execution_summary"] == {
        "total": 3,
        "succeeded": 0,
        "partial": 0,
        "failed": 3,
        "not_run": 0,
        "requires_human": 0,
        "degraded": False,
        "all_failed": True,
        "provider_failed": True,
        "no_execution": False,
        "retrieval_incomplete_budget": False,
    }
    projected = tool.project_model_context(observation)
    assert projected["succeeded"] is False
    assert projected["error"] == observation.error
    assert projected["execution_summary"]["failed"] == 3


def test_failed_request_with_unrun_followups_remains_failure():
    summary = _summarize_search_executions([
        execution(SearchExecutionStatus.FAILED, 1),
        execution(SearchExecutionStatus.NOT_RUN, 2),
    ])
    assert summary["all_failed"] is True
    assert summary["no_execution"] is False
    assert summary["not_run"] == 1


def test_successful_empty_is_not_execution_failure(tmp_path):
    _, observation = observe_bundle(
        tmp_path, bundle(SearchExecutionStatus.SUCCEEDED)
    )

    assert observation.succeeded is True
    assert observation.payload["database_search_result"]["results"] == []
    assert observation.error is None


def test_mixed_execution_is_succeeded_and_degraded(tmp_path):
    _, observation = observe_bundle(
        tmp_path,
        bundle(
            SearchExecutionStatus.SUCCEEDED,
            SearchExecutionStatus.FAILED,
            SearchExecutionStatus.FAILED,
            with_result=True,
        ),
    )

    summary = observation.payload["execution_summary"]
    assert observation.succeeded is True
    assert len(observation.payload["database_search_result"]["results"]) == 1
    assert summary["degraded"] is True
    assert summary["failed"] == 2
    assert any(
        "2/3 search executions failed" in warning
        for warning in observation.payload["database_search_result"]["warnings"]
    )


def test_partial_execution_is_succeeded(tmp_path):
    _, observation = observe_bundle(tmp_path, bundle(SearchExecutionStatus.PARTIAL))

    assert observation.succeeded is True
    assert observation.payload["execution_summary"]["partial"] == 1


def test_no_execution_is_structured_failure(tmp_path):
    _, observation = observe_bundle(tmp_path, bundle())

    assert observation.succeeded is False
    assert observation.error.startswith("no_search_execution:")
    assert observation.payload["execution_summary"]["no_execution"] is True


def test_scope_planning_persistence_projection_and_deduplication(tmp_path):
    tool, store, planner = build_tool(tmp_path)
    observation = asyncio.run(
        tool.ainvoke(DatabaseSearchArguments(source_id="demo"), scope=scope())
    )
    second = asyncio.run(
        tool.ainvoke(DatabaseSearchArguments(source_id="demo"), scope=scope())
    )

    assert planner.calls == []
    assert observation.succeeded and observation.tool_name == "database_search"
    assert "bundle" not in observation.payload
    assert "search_executions" in observation.payload
    assert observation.payload["source_records"][0]["raw_metadata"]
    result = observation.payload["database_search_result"]
    assert len(result["results"]) == 1
    assert result["results"][0]["artifact_ids"]

    manifest = store.load_manifest(scope().subject_paper_id)
    assert len(manifest.works) == 1
    assert len(manifest.source_records) == 1
    assert len(manifest.artifacts) == 2
    assert second.payload["database_search_result"]["results"][0]["work_id"] == manifest.works[0].work_id

    projected = tool.project_model_context(observation)
    serialized = json.dumps(projected)
    assert projected["source_id"] == "demo"
    assert "relative_path" not in serialized
    assert "raw_metadata" not in serialized
    assert "ResearchBundle" not in serialized and "bundle" not in serialized
    assert "private_provider_field" not in serialized


def test_constructor_requires_one_shared_store(tmp_path):
    tool, store, _ = build_tool(tmp_path)
    internal = tool.tools_by_source["demo"]
    with pytest.raises(ValueError, match="share reference_store"):
        DatabaseSearchTool({"demo": internal}, ReferenceStore(tmp_path / "other"))
