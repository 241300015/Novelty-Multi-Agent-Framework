"""Offline production-path smoke from search through rendered report."""

from __future__ import annotations

import json
import re
import hashlib
from pathlib import Path

from backend.env import ModelResponse, ModelToolCall
from novelty_agent_framework.agents import DemoCoordinator, DemoPointExtractor, DemoSearchPlanner
from novelty_agent_framework.core import RuntimeDebugConfig
from novelty_agent_framework.persistence import ReferenceStore
from novelty_agent_framework.ports import SearchHit
from novelty_agent_framework.schemas import (NoveltyPointReview, NoveltyVerdict,
    PaperInput, ReviewStatus)
from novelty_agent_framework.tools import ResearcherToolRegistry
from novelty_agent_framework.tools.database_search import (
    DatabaseSearchTool, RetrievalSource, StructuredSourceRetrievalTool,
)
from novelty_agent_framework.tools.reader import ReaderTool
from novelty_agent_framework.tools.reference_reader import ReferenceArtifactReaderTool
from novelty_agent_framework.tools.evidence_card_builder import EvidenceCardBuilder
from novelty_agent_framework.agents import DemoQueryAdapter
from novelty_agent_framework.workflows import (
    NoveltyWorkflow, NoveltyWorkflowConfig, NoveltyWorkflowServices,
    TaskResearcherWorkflow,
)


class Searcher:
    source_id = "demo"

    def __init__(self):
        self.calls = []

    async def search(self, query: str, *, limit: int = 10):
        self.calls.append((query, limit))
        return [SearchHit(document_id="fixture-1", source_id="demo",
                          title="Fixture paper", abstract="Grounded quote appears here.",
                          url="https://example.test/fixture-1")]


class ScriptedModel:
    def __init__(self):
        self.tool_arguments = []

    async def acomplete(self, messages, *, options=None):
        tool_messages = [message.content for message in messages if message.role == "tool"]
        if not tool_messages:
            self.tool_arguments.append(("database_search", {"source_id": "demo"}))
            return ModelResponse(content=None, tool_calls=(ModelToolCall(
                id="db-call", name="database_search", arguments={"source_id": "demo"}),))
        if len(tool_messages) == 1:
            match = re.search(r"art_[a-f0-9]{24}", str(tool_messages[-1]))
            assert match, tool_messages[-1]
            self.tool_arguments.append(("reader", {"artifact_id": match.group()}))
            return ModelResponse(content=None, tool_calls=(ModelToolCall(
                id="reader-call", name="reader", arguments={"artifact_id": match.group()}),))
        return ModelResponse(content=json.dumps({"cards": [{
            "main_contribution": "Grounded contribution",
            "overlaps": ["shared feature"], "differences": ["different setting"],
            "quotes": [{"quote": "Grounded quote", "interpretation": "fixture",
                        "confidence": 0.9}],
            "relevance": 0.9, "confidence": 0.9,
        }]}))


class AcceptReviewer:
    def review(self, request):
        return NoveltyPointReview(
            novelty_point_id=request.novelty_point.point_id,
            status=ReviewStatus.REVIEWED, verdict=NoveltyVerdict.NOVEL,
            verdict_reason="fixture judgment", confidence=0.5,
        )


def test_production_path_exports_search_read_card_review_and_report(tmp_path, monkeypatch):
    def forbid_network(*args, **kwargs):
        raise AssertionError("offline retrieval smoke must not access external services")
    monkeypatch.setattr("socket.getaddrinfo", forbid_network)
    output = tmp_path / "output"
    store = ReferenceStore(output)
    searcher = Searcher()
    model = ScriptedModel()
    retrieval = StructuredSourceRetrievalTool(
        source=RetrievalSource(source_id="demo", query_adapter=DemoQueryAdapter(),
                               search_tool=searcher),
        reference_store=store, candidate_limit=2, per_query_limit=2,
        full_text_limit=0, max_provider_requests=6,
    )
    tools = ResearcherToolRegistry([
        DatabaseSearchTool({"demo": retrieval}, store),
        ReaderTool(ReferenceArtifactReaderTool(store)),
    ])
    workflow = NoveltyWorkflow(
        NoveltyWorkflowServices(
            coordinator=DemoCoordinator(), point_extractor=DemoPointExtractor(),
            search_planner=DemoSearchPlanner(),
            task_researcher=TaskResearcherWorkflow(model, tools,
                                                   EvidenceCardBuilder(store)),
            reviewer=AcceptReviewer(),
        ),
        config=NoveltyWorkflowConfig(max_rounds=1, max_concurrency=1, metadata_verification_enabled=False,
            runtime_debug=RuntimeDebugConfig(
                output_root=output, archive_root=tmp_path / "archive")),
        output_root=output,
    )
    result = workflow.run(PaperInput(
        paper_id="smoke-paper", title="Smoke paper", abstract="Abstract",
        full_text="Main text", claimed_contributions=["grounded claim"]))
    assert result.evidence_cards
    assert result.report
    archive = next((tmp_path / "archive").glob("smoke-paper_*/run-*"))
    assert list((archive / "retrieval_events").glob("*.json"))
    tools_recorded = [json.loads(path.read_text()) for path in
                      (archive / "tools").glob("*.json")]
    assert {item["tool_name"] for item in tools_recorded} >= {"database_search", "reader"}
    assert list((archive / "stages").glob("*_review_evidence/output.json"))
    assert list((archive / "stages").glob("*_render_report/output.json"))
    exported_store = ReferenceStore(archive / "workspace")
    exported_manifest = exported_store.load_manifest("smoke-paper")
    assert exported_manifest.artifacts
    assert (archive / "workspace" / "smoke-paper" / "report.json").is_file()
    manifest = json.loads((archive / "manifest.json").read_text())
    assert (archive / manifest["llm_pricing_path"]).is_file()

    def check_references(value):
        if isinstance(value, dict):
            if value.get("type") in {"content_reference", "artifact_reference"}:
                relative = Path(value["path"])
                assert not relative.is_absolute()
                payload = archive / relative
                assert payload.is_file()
                assert hashlib.sha256(payload.read_bytes()).hexdigest() == value["sha256"]
            for child in value.values():
                check_references(child)
        elif isinstance(value, list):
            for child in value:
                check_references(child)

    for path in archive.rglob("*.json"):
        check_references(json.loads(path.read_text()))

    # Run the same production graph with tracing disabled and identical stubs.
    output_without_trace = tmp_path / "output-no-trace"
    store_without_trace = ReferenceStore(output_without_trace)
    searcher_without_trace = Searcher()
    model_without_trace = ScriptedModel()
    retrieval_without_trace = StructuredSourceRetrievalTool(
        source=RetrievalSource(source_id="demo", query_adapter=DemoQueryAdapter(),
                               search_tool=searcher_without_trace),
        reference_store=store_without_trace, candidate_limit=2, per_query_limit=2,
        full_text_limit=0, max_provider_requests=6,
    )
    tools_without_trace = ResearcherToolRegistry([
        DatabaseSearchTool({"demo": retrieval_without_trace}, store_without_trace),
        ReaderTool(ReferenceArtifactReaderTool(store_without_trace)),
    ])
    workflow_without_trace = NoveltyWorkflow(
        NoveltyWorkflowServices(
            coordinator=DemoCoordinator(), point_extractor=DemoPointExtractor(),
            search_planner=DemoSearchPlanner(),
            task_researcher=TaskResearcherWorkflow(
                model_without_trace, tools_without_trace,
                EvidenceCardBuilder(store_without_trace)),
            reviewer=AcceptReviewer(),
        ),
        config=NoveltyWorkflowConfig(max_rounds=1, max_concurrency=1, metadata_verification_enabled=False,
            runtime_debug=RuntimeDebugConfig(enabled=False,
                output_root=output_without_trace,
                archive_root=tmp_path / "archive-no-trace")),
        output_root=output_without_trace,
    )
    result_without_trace = workflow_without_trace.run(PaperInput(
        paper_id="smoke-paper", title="Smoke paper", abstract="Abstract",
        full_text="Main text", claimed_contributions=["grounded claim"]))
    assert searcher.calls == searcher_without_trace.calls
    assert model.tool_arguments == model_without_trace.tool_arguments
    assert [card.card_id for card in result.evidence_cards] == [
        card.card_id for card in result_without_trace.evidence_cards]
