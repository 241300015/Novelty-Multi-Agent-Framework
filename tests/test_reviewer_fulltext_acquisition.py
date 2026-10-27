"""Local read -> known-record acquisition -> verified read; no live provider or LLM."""
import asyncio
from dataclasses import replace

import pytest

from novelty_agent_framework.agents.evidence_reviewer import EvidenceReviewerConfig, NoveltyEvidenceReviewer
from novelty_agent_framework.ports import FullText
from novelty_agent_framework.schemas import (
    DatabaseSearchArguments, NoveltyPointReviewRequest, ReferenceReadRequest,
    ResearchFinishDraft, ArtifactRole,
)
from novelty_agent_framework.tools import (
    EvidenceCardBuilder, ReviewerReaderTool, ReferenceArtifactReaderTool, ResearcherToolRegistry,
)
from novelty_agent_framework.tools.reviewer_database_search import (
    ReviewerDatabaseSearchTool, ReviewerAcquisitionScope,
)
from novelty_agent_framework.workflows.reviewer_reading_policy import ReviewerReadingPolicy
from test_database_search_tool import build_tool, scope
from test_reviewer_controlled_harness import ChoosingClient, row, select
from test_reviewer_summary_checkpoint import Client, output


BODY = "The complete original body supports a concrete graph mechanism."


def prepare(tmp_path, *, saved_body=False, failure=None):
    database, store, planner = build_tool(tmp_path)
    research = scope()
    retrieval = database.tools_by_source["demo"]
    retrieval.full_text_limit = 0
    events = []

    class FullTexts:
        source_id = "demo"

        async def fetch(self, document_id):
            events.append(("fetch", document_id))
            if failure == "empty":
                return None
            if failure == "error":
                raise TimeoutError("injected full-text timeout")
            return FullText(document_id=document_id, title="Candidate", text=BODY, content_extent="full")

    retrieval.source = replace(retrieval.source, full_text_tool=FullTexts())

    async def seed():
        result = await database.ainvoke(DatabaseSearchArguments(source_id="demo"), scope=research)
        item = result.payload["database_search_result"]["results"][0]
        abstract = next(a for a in store.load_manifest(research.subject_paper_id).artifacts
                        if a.source_record_id == item["source_record_id"] and a.role == ArtifactRole.ABSTRACT)
        read = await ReferenceArtifactReaderTool(store).ainvoke(ReferenceReadRequest(
            subject_paper_id=research.subject_paper_id, artifact_id=abstract.artifact_id,
            namespace="research_reference", char_start=0, max_chars=1000))
        built = EvidenceCardBuilder(store).build(ResearchFinishDraft(cards=[{
            "main_contribution": "Candidate mechanism", "relevance": 0.8, "confidence": 0.8,
            "quotes": [{"quote": read.text, "interpretation": "Candidate fact", "confidence": 0.8}]}]),
            scope=research, read_results=[read])
        assert built.evidence_cards
        request = NoveltyPointReviewRequest(subject_paper_id=research.subject_paper_id,
            novelty_point=research.novelty_point, tasks=[research.research_task],
            cards=built.evidence_cards, evidence=built.evidence)
        if saved_body:
            await database.ainvoke(DatabaseSearchArguments(source_id="demo",
                full_text_source_record_ids=[item["source_record_id"]]), scope=research)
        return request, item

    request, item = asyncio.run(seed())
    events.clear()

    class NoSearch:
        source_id = "demo"

        def search(self, *args, **kwargs):
            raise AssertionError("Reviewer must not restart search")

    retrieval.source = replace(retrieval.source, search_tool=NoSearch())

    class Reader(ReviewerReaderTool):
        async def ainvoke(self, arguments, *, scope):
            events.append(("reader", arguments.artifact_id))
            return await super().ainvoke(arguments, scope=scope)

    reader = Reader(ReferenceArtifactReaderTool(store))
    registry = ResearcherToolRegistry([reader, ReviewerDatabaseSearchTool(database)])
    return request, registry, store, item, events


def run_card(request, registry, choices, **budgets):
    client = ChoosingClient(request, choices)
    reviewer = NoveltyEvidenceReviewer(client, tool_registry=registry,
        config=EvidenceReviewerConfig(guided_reading=True, max_steps=14,
            max_tool_calls=budgets.get("max_tool_calls", 6), max_total_read_chars=32_000))
    result = asyncio.run(reviewer.review_card(request))
    assert result.status.value == "reviewed", result.model_dump()
    return result, reviewer, client


def test_fetch_only_after_local_read_and_register_new_quote_in_summary_checkpoint(tmp_path):
    request, registry, store, item, events = prepare(tmp_path)
    review, reviewer, _ = run_card(request, registry,
        ["待核验", "尝试回读本地材料", "在线获取", "读取新获取", "可用", "回读有帮助"])
    assert [event[0] for event in events] == ["reader", "fetch", "reader"]
    assert events[0][1] != events[2][1]
    assert len(review.review_evidence) == 2
    assert any(e.exact_quote == BODY for e in review.review_evidence)
    assert [r["origin"] for r in review.harness_state["read_history"]] == ["local", "acquired"]
    assert review.harness_state["artifact_read_complete"]
    assert review.harness_state["acquisition_history"][0]["outcome"] == "readable_fulltext"
    stored = store.find_artifact(request.subject_paper_id, events[2][1])
    assert stored.source_record_id == item["source_record_id"]
    reviewer.model_client = Client(output(request, reviewed=True))
    summary = asyncio.run(reviewer.summarize_with_checkpoint(request, [row(request, review)],
        output_root=tmp_path, run_id="acquired-summary"))
    assert summary.durable and summary.status == "completed"
    assert summary.review.review_evidence == review.review_evidence


def test_saved_readable_body_avoids_acquisition(tmp_path):
    request, registry, _, _, events = prepare(tmp_path, saved_body=True)
    review, _, _ = run_card(request, registry, ["可用", "继续阅读正文", "可用", "回读没有新增"])
    assert [e[0] for e in events] == ["reader"]
    assert not review.harness_state["acquisition_history"]


def test_missing_saved_body_is_attempted_then_reacquired(tmp_path):
    request, registry, store, _, events = prepare(tmp_path, saved_body=True)
    body = next(a for a in store.load_manifest(request.subject_paper_id).artifacts
                if a.role == ArtifactRole.EXTRACTED_TEXT)
    _, path, _ = store.verify_artifact_file(request.subject_paper_id, body.artifact_id)
    path.unlink()
    review, _, _ = run_card(request, registry,
        ["待核验", "继续阅读正文", "在线获取", "读取新获取", "可用", "回读有帮助"])
    assert [e[0] for e in events] == ["reader", "fetch", "reader"]
    assert any(issue.code == "reader.file.missing" for issue in review.execution_issues)
    assert not review.harness_state["read_history"][0]["succeeded"]
    assert review.harness_state["read_history"][1]["succeeded"]
    assert path.read_text() == BODY


@pytest.mark.parametrize("failure", ["empty", "error"])
def test_failed_acquisition_preserves_original_material_without_retry_loop(tmp_path, failure):
    request, registry, _, _, events = prepare(tmp_path, failure=failure)
    review, _, _ = run_card(request, registry,
        ["待核验", "尝试回读本地材料", "在线获取", "待核验", "回读效果尚无法判断"])
    assert [e[0] for e in events] == ["reader", "fetch"]
    assert review.harness_state["acquisition_history"][0]["outcome"] == "no_readable_authorized_fulltext"
    assert all(e.exact_quote != BODY for e in review.review_evidence)
    assert review.execution_issues


def test_fetch_is_optional_and_requires_budget_for_subsequent_read(tmp_path):
    request, registry, _, _, events = prepare(tmp_path)
    review, _, _ = run_card(request, registry,
        ["待核验", "尝试回读本地材料", "不在线获取", "待核验", "回读效果尚无法判断"])
    assert [e[0] for e in events] == ["reader"]
    assert not review.harness_state["acquisition_history"]
    events.clear()
    review, _, _ = run_card(request, registry,
        ["待核验", "尝试回读本地材料", "待核验", "回读效果尚无法判断"], max_tool_calls=2)
    assert [e[0] for e in events] == ["reader"]


def test_registry_rejects_discovery_foreign_source_and_missing_local_permission(tmp_path):
    request, registry, _, item, events = prepare(tmp_path)
    artifact_id = request.evidence[0].artifact_id
    permission = ReviewerAcquisitionScope(request, "demo", item["source_record_id"], (artifact_id,))
    async def check():
        for arguments, scope_value in [
            ({"source_id": "demo"}, permission),
            ({"source_id": "demo", "full_text_source_record_ids": ["foreign"]}, permission),
            ({"source_id": "demo", "full_text_source_record_ids": [item["source_record_id"]]}, request),
            ({"source_id": "demo", "full_text_source_record_ids": [item["source_record_id"]]},
             replace(permission, local_attempt_artifact_ids=("foreign-artifact",))),
        ]:
            result = await registry.execute("database_search", arguments, scope=scope_value)
            assert not result.succeeded
    asyncio.run(check())
    assert events == []


def test_initial_and_reading_decision_menus_never_offer_acquisition(tmp_path):
    request, registry, _, _, events = prepare(tmp_path)
    policy = ReviewerReadingPolicy(registry, request, catalog=registry.get("reader").material_catalog(request),
        max_read_chars=10_000, max_tool_calls=4)
    assert not any("在线获取" in v for v in policy.choices().values())
    select(policy, "待核验")
    assert not any("在线获取" in v for v in policy.choices().values())
    select(policy, "不继续")
    assert policy.can_finish and events == []


def test_acquired_but_unread_text_never_becomes_evidence_on_model_budget_stop(tmp_path):
    request, registry, _, _, events = prepare(tmp_path)
    client = ChoosingClient(request, ["待核验", "尝试回读本地材料", "在线获取"])
    reviewer = NoveltyEvidenceReviewer(client, tool_registry=registry,
        config=EvidenceReviewerConfig(guided_reading=True, max_steps=4, max_tool_calls=6))
    result = asyncio.run(reviewer.review_card(request))
    assert [e[0] for e in events] == ["reader", "fetch"]
    assert result.status.value == "insufficient_evidence" and result.incomplete_reason == "budget_exhausted"
    assert result.harness_state["stage"] == "read_acquired"
    assert result.harness_state["acquisition_history"][0]["outcome"] == "readable_fulltext"
    assert not any(e.exact_quote == BODY for e in result.review_evidence)


def test_corrupt_body_is_not_counted_as_a_valid_cache_hit_or_silently_overwritten(tmp_path):
    request, registry, store, _, events = prepare(tmp_path, saved_body=True)
    body = next(a for a in store.load_manifest(request.subject_paper_id).artifacts
                if a.role == ArtifactRole.EXTRACTED_TEXT)
    _, path, _ = store.verify_artifact_file(request.subject_paper_id, body.artifact_id)
    path.write_text("damaged file")
    review, _, _ = run_card(request, registry,
        ["待核验", "继续阅读正文", "在线获取", "待核验", "回读效果尚无法判断"])
    assert [e[0] for e in events] == ["reader", "fetch"]
    assert path.read_text() == "damaged file"
    assert not review.review_evidence
    assert review.harness_state["acquisition_history"][0]["outcome"] == "no_readable_authorized_fulltext"


@pytest.mark.parametrize("legacy", [False, True])
def test_factories_register_acquisition_adapter_with_the_same_store(legacy):
    from novelty_agent_framework.config import load_application_config, build_workflow
    from novelty_agent_framework.config.loader import legacy_shape
    config = load_application_config(environ={})
    reviewer = build_workflow(legacy_shape(config) if legacy else config).services.reviewer
    assert set(reviewer.tools.names) == {"reader", "database_search"}
    database = reviewer.tools.get("database_search")
    assert isinstance(database, ReviewerDatabaseSearchTool)
    assert database.reference_store is reviewer.tools.get("reader").reader.reference_store
