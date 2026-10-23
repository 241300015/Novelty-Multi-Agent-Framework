from __future__ import annotations

import asyncio
import json

from backend.env import ModelResponse, ModelToolCall

from novelty_agent_framework.agents import DemoQueryAdapter
from novelty_agent_framework.core import RuntimeArtifactManager, RuntimeDebugConfig
from novelty_agent_framework.persistence import ReferenceStore
from novelty_agent_framework.ports import SearchHit as DatabaseHit
from novelty_agent_framework.schemas import (
    NoveltyPoint,
    ResearchTask,
    SearchConcept,
    SearchPlan,
    SearchStrategy,
    TaskResearchRequest,
)
from novelty_agent_framework.tools import (
    BrowserTool,
    EvidenceCardBuilder,
    ReaderTool,
    ReferenceArtifactReaderTool,
    ResearcherToolRegistry,
    WebSearchTool,
)
from novelty_agent_framework.tools.browser_backend import BrowserFetchResult
from novelty_agent_framework.tools.database_search import (
    DatabaseSearchTool,
    RetrievalSource,
    StructuredSourceRetrievalTool,
)
from novelty_agent_framework.tools.web_search_backend import SearchBackendResult, SearchHit
from novelty_agent_framework.workflows import TaskResearcherConfig, TaskResearcherWorkflow
from conftest import minimal_search_plan

DB_TEXT = "Database abstract supports temporal graph summarization."
WEB_TEXT = "Web page confirms the documented baseline behavior."


class Planner:
    def plan(self, point, task):
        return SearchPlan(
            task_id=task.task_id,
            novelty_point_id=point.point_id,
            concepts=[SearchConcept(concept_id="C1", name="graph", terms=["graph"])],
            strategies=[SearchStrategy(strategy_id="S1", level="strict", expression="C1")],
        )


class FailingLegacyPlanner:
    def plan(self, *args, **kwargs):
        raise AssertionError("legacy planner path must not be called")


class DatabaseBackend:
    source_id = "demo"

    def search(self, query, *, limit=10):
        return [DatabaseHit(
            document_id="db-1", source_id="demo", title="Database Work",
            abstract=DB_TEXT, authors=("Alice",), year=2024,
            url="https://db.example/work/1",
        )]


class FailingDatabaseBackend:
    source_id = "demo"

    def search(self, query, *, limit=10):
        raise TimeoutError(f"database timed out for {query}")


class WebBackend:
    name = "fixture-web"

    async def search(self, query, *, max_results):
        return SearchBackendResult(query=query, hits=[SearchHit(
            title="Web Work", url="https://web.example/work/1", snippet="candidate"
        )])


class BrowserBackend:
    name = "fixture-browser"

    async def fetch(self, url):
        return BrowserFetchResult(
            requested_url=url, final_url=url, title="Web Work",
            html=f"<p>{WEB_TEXT}</p>", text=WEB_TEXT, content_type="text/html",
        )


class ScriptedFullToolModel:
    def __init__(self):
        self.step = 0
        self.tool_sequence = []
        self.options = []

    async def acomplete(self, messages, *, options=None):
        self.options.append(options)
        if self.step == 0:
            response = self._call("db", "database_search", {"source_id": "demo"})
        elif self.step == 1:
            payload = json.loads(messages[-1].content)
            artifact_id = payload["results"][0]["artifact_ids"][0]
            response = self._call("read-db", "reader", {"artifact_id": artifact_id})
        elif self.step == 2:
            response = self._call("web", "web_search", {"query": "baseline", "max_results": 1})
        elif self.step == 3:
            payload = json.loads(messages[-1].content)
            source_id = payload["results"][0]["source_record_id"]
            response = self._call("browse", "browser", {"source_record_id": source_id})
        elif self.step == 4:
            payload = json.loads(messages[-1].content)
            artifact_id = payload["artifacts"][0]["artifact_id"]
            response = self._call("read-web", "reader", {"artifact_id": artifact_id})
        else:
            response = ModelResponse(content=json.dumps({
                "cards": [
                    {
                        "main_contribution": "Database candidate",
                        "overlaps": ["temporal graph"], "differences": [],
                        "quotes": [{"quote": DB_TEXT, "interpretation": "DB support", "confidence": 0.9}],
                        "possible_baseline": True, "relevance": 0.9, "confidence": 0.9,
                    },
                    {
                        "main_contribution": "Web candidate",
                        "overlaps": ["baseline"], "differences": [],
                        "quotes": [{"quote": WEB_TEXT, "interpretation": "Web support", "confidence": 0.8}],
                        "possible_baseline": True, "relevance": 0.8, "confidence": 0.8,
                    },
                ],
                "no_evidence_reason": None,
            }))
        self.step += 1
        return response

    def _call(self, call_id, name, arguments):
        self.tool_sequence.append(name)
        return ModelResponse(content=None, tool_calls=[ModelToolCall(call_id, name, arguments)])


class DatabaseReaderModel(ScriptedFullToolModel):
    async def acomplete(self, messages, *, options=None):
        self.options.append(options)
        if self.step == 0:
            response = self._call("db", "database_search", {"source_id": "demo"})
        elif self.step == 1:
            payload = json.loads(messages[-1].content)
            response = self._call(
                "read-db",
                "reader",
                {"artifact_id": payload["results"][0]["artifact_ids"][0]},
            )
        else:
            response = ModelResponse(
                content=json.dumps(
                    {
                        "cards": [
                            {
                                "main_contribution": "Database candidate",
                                "overlaps": ["temporal graph"],
                                "differences": [],
                                "quotes": [
                                    {
                                        "quote": DB_TEXT,
                                        "interpretation": "DB support",
                                        "confidence": 0.9,
                                    }
                                ],
                                "possible_baseline": True,
                                "relevance": 0.9,
                                "confidence": 0.9,
                            }
                        ],
                        "no_evidence_reason": None,
                    }
                )
            )
        self.step += 1
        return response


class FailureAwareModel:
    def __init__(self):
        self.step = 0
        self.failure_context = None

    async def acomplete(self, messages, *, options=None):
        if self.step == 0:
            response = ModelResponse(
                content=None,
                tool_calls=[
                    ModelToolCall(
                        "db-failed", "database_search", {"source_id": "demo"}
                    )
                ],
            )
        else:
            self.failure_context = json.loads(messages[-1].content)
            response = ModelResponse(
                content=json.dumps(
                    {
                        "cards": [],
                        "no_evidence_reason": "database execution failed",
                    }
                )
            )
        self.step += 1
        return response


def test_single_task_uses_scope_plan_without_legacy_planner(tmp_path):
    store = ReferenceStore(tmp_path)
    internal = StructuredSourceRetrievalTool(
        search_planner=FailingLegacyPlanner(),
        source=RetrievalSource(
            source_id="demo",
            query_adapter=DemoQueryAdapter(),
            search_tool=DatabaseBackend(),
        ),
        reference_store=store,
    )
    registry = ResearcherToolRegistry(
        [
            DatabaseSearchTool({"demo": internal}, store),
            ReaderTool(ReferenceArtifactReaderTool(store)),
        ]
    )
    model = DatabaseReaderModel()
    workflow = TaskResearcherWorkflow(
        model,
        registry,
        EvidenceCardBuilder(store),
        config=TaskResearcherConfig(max_steps=4, max_tool_calls=3),
    )
    request = TaskResearchRequest(
        subject_paper_id="single-task",
        run_id="single-task-run",
        novelty_point=NoveltyPoint(point_id="NP-1", claim="graph novelty"),
        research_task=ResearchTask(
            task_id="T-1",
            novelty_point_id="NP-1",
            task_type="search",
            language="en",
        ),
        search_plan=minimal_search_plan("T-1", "NP-1"),
    )

    result = asyncio.run(workflow.ainvoke(request))

    assert registry.names == ("database_search", "reader")
    assert model.tool_sequence == ["database_search", "reader"]
    assert len(result.research_bundles) == 1
    assert len(result.research_bundles[0].search_executions) == 1
    assert result.research_bundles[0].search_executions[0].parameters[
        "strategy_id"
    ] == request.search_plan.strategies[0].strategy_id
    assert len(result.read_results) == 1
    assert len(result.evidence_cards) == 1


def test_scripted_four_tool_chain_shares_handles_and_builds_evidence(tmp_path):
    store = ReferenceStore(tmp_path)
    database_internal = StructuredSourceRetrievalTool(
        search_planner=Planner(),
        source=RetrievalSource(
            source_id="demo", query_adapter=DemoQueryAdapter(), search_tool=DatabaseBackend()
        ),
        reference_store=store,
    )
    database = DatabaseSearchTool({"demo": database_internal}, store)
    web = WebSearchTool(WebBackend(), store)
    browser = BrowserTool(BrowserBackend(), store)
    reader = ReaderTool(ReferenceArtifactReaderTool(store))
    registry = ResearcherToolRegistry([database, web, browser, reader])
    model = ScriptedFullToolModel()
    workflow = TaskResearcherWorkflow(
        model, registry, EvidenceCardBuilder(store),
        config=TaskResearcherConfig(max_steps=8, max_tool_calls=7),
    )
    request = TaskResearchRequest(
        subject_paper_id="paper-four-tools", run_id="run-four-tools",
        novelty_point=NoveltyPoint(point_id="NP-1", claim="graph novelty", technical_features=["graph"]),
        research_task=ResearchTask(task_id="T-1", novelty_point_id="NP-1", task_type="search", language="en"),
        search_plan=minimal_search_plan("T-1", "NP-1"),
    )

    result = asyncio.run(workflow.ainvoke(request))
    manifest = store.load_manifest(request.subject_paper_id)

    assert registry.names == ("database_search", "web_search", "browser", "reader")
    assert model.tool_sequence == ["database_search", "reader", "web_search", "browser", "reader"]
    assert {tool.name for tool in model.options[0].tools} == set(registry.names)
    assert "structured_source_retrieval" not in {tool.name for tool in model.options[0].tools}
    assert len(result.read_results) == 2
    assert len(result.evidence) == len(result.evidence_cards) == 1
    assert result.evidence[0].quote == DB_TEXT
    assert any("web supplementary material" in warning for warning in result.warnings)
    assert result.steps_used == 6  # Web policy rejection does not trigger quote repair.
    assert len(result.research_bundles) == 1
    assert len(manifest.works) == 2
    assert len(manifest.source_records) == 2
    assert len(manifest.artifacts) == 2
    assert database.reference_store is web.reference_store is browser.reference_store is reader.reader.reference_store is store


def test_failed_database_observation_reaches_model_runtime_and_is_not_trusted(
    tmp_path,
):
    store = ReferenceStore(tmp_path / "references")
    internal = StructuredSourceRetrievalTool(
        source=RetrievalSource(
            source_id="demo",
            query_adapter=DemoQueryAdapter(),
            search_tool=FailingDatabaseBackend(),
        ),
        reference_store=store,
    )
    registry = ResearcherToolRegistry(
        [DatabaseSearchTool({"demo": internal}, store)]
    )
    model = FailureAwareModel()
    workflow = TaskResearcherWorkflow(
        model,
        registry,
        EvidenceCardBuilder(store),
        config=TaskResearcherConfig(max_steps=3, max_tool_calls=2),
    )
    request = TaskResearchRequest(
        subject_paper_id="failed-database",
        run_id="failed-database-run",
        novelty_point=NoveltyPoint(point_id="NP-1", claim="graph novelty"),
        research_task=ResearchTask(
            task_id="T-1",
            novelty_point_id="NP-1",
            task_type="search",
            language="en",
        ),
        search_plan=minimal_search_plan("T-1", "NP-1"),
    )
    manager = RuntimeArtifactManager(
        request.subject_paper_id,
        config=RuntimeDebugConfig(
            output_root=tmp_path / "outputs",
            archive_root=tmp_path / "archive",
        ),
        run_id=request.run_id,
        enabled_tools=["database_search"],
    )
    manager.activate()
    try:
        result = asyncio.run(workflow.ainvoke(request))
    finally:
        manager.deactivate()
    manager.finish_run("SUCCESS")

    assert result.research_bundles == []
    assert model.failure_context["succeeded"] is False
    assert "search executions failed" in model.failure_context["error"]
    assert model.failure_context["execution_summary"]["all_failed"] is True
    record = json.loads(
        next((manager.run_dir / "tools").glob("*.json")).read_text(encoding="utf-8")
    )
    assert record["execution_status"] == "FAILED"
    assert record["business_status"] != "EMPTY"
    assert record["failure_phase"] == "TOOL_EXECUTION"
    assert record["raw_result"]["payload"]["research_bundle"]
    summary = json.loads((manager.run_dir / "summary.json").read_text(encoding="utf-8"))
    # Original failed request plus one bounded task-end retry are both audited.
    assert summary["tool_calls"] == [
        {
            "calls": 2,
            "empty": 0,
            "failed": 2,
            "success": 0,
            "tool_name": "database_search",
        }
    ]
