"""Reviewer acquisition is bounded to one Card's persisted source records."""

import asyncio

import pytest

from novelty_agent_framework.schemas import (
    DatabaseSearchArguments,
    Evidence,
    EvidenceCard,
    NoveltyPointReviewRequest,
)
from novelty_agent_framework.tools import (
    ReferenceArtifactReaderTool,
    ReviewerDatabaseSearchTool,
    ReviewerReaderTool,
)
from test_database_search_tool import build_tool, scope


def test_reviewer_can_only_acquire_current_card_record_and_catalog_refreshes(tmp_path):
    database, store, planner = build_tool(tmp_path)
    retrieval = database.tools_by_source["demo"]
    retrieval.full_text_limit = 0

    async def run():
        research_scope = scope()
        discovery = await database.ainvoke(
            DatabaseSearchArguments(source_id="demo"), scope=research_scope
        )
        item = discovery.payload["database_search_result"]["results"][0]
        assert not item["full_text_artifact_ids"]
        evidence = Evidence(
            evidence_id="ev-reviewer-acquisition",
            work_id=item["work_id"],
            artifact_id=item["artifact_ids"][0],
            novelty_point_id="NP-1",
            task_id="T-1",
            quote="database abstract",
            interpretation="fixture",
            confidence=0.8,
            provenance={
                "artifact_namespace": "research_reference",
                "source_record_id": item["source_record_id"],
            },
        )
        card = EvidenceCard(
            card_id="card-reviewer-acquisition",
            task_id="T-1",
            novelty_point_id="NP-1",
            document_title=item["title"],
            main_contribution="fixture",
            relevance=0.8,
            confidence=0.8,
            evidence_ids=[evidence.evidence_id],
        )
        request = NoveltyPointReviewRequest(
            subject_paper_id="subject-1",
            run_id="run-1",
            novelty_point=research_scope.novelty_point,
            tasks=[research_scope.research_task],
            cards=[card],
            evidence=[evidence],
        )
        reviewer_database = ReviewerDatabaseSearchTool(database)
        reviewer_reader = ReviewerReaderTool(ReferenceArtifactReaderTool(store))
        before = reviewer_reader.material_catalog(request)
        assert not any(row["role"] == "extracted_text" for row in before)

        with pytest.raises(PermissionError, match="cannot execute discovery"):
            await reviewer_database.ainvoke(
                DatabaseSearchArguments(source_id="demo"), scope=request
            )
        with pytest.raises(PermissionError, match="outside the current Card"):
            await reviewer_database.ainvoke(
                DatabaseSearchArguments(
                    source_id="demo",
                    full_text_source_record_ids=["src-other-card"],
                ),
                scope=request,
            )

        acquired = await reviewer_database.ainvoke(
            DatabaseSearchArguments(
                source_id="demo",
                full_text_source_record_ids=[item["source_record_id"]],
            ),
            scope=request,
        )
        assert acquired.succeeded
        assert acquired.payload["search_executions"] == []
        assert acquired.payload["acquisition_results"][0]["status"] == "acquired"
        after = reviewer_reader.material_catalog(request)
        fulltext_ids = {
            row["artifact_id"] for row in after
            if row["role"] == "extracted_text" and row["readable"]
        }
        assert fulltext_ids == set(
            acquired.payload["acquisition_results"][0]["full_text_artifact_ids"]
        )

    asyncio.run(run())
    assert planner.calls == []


def test_reviewer_rejects_record_bound_to_another_card_work(tmp_path):
    database, _, _ = build_tool(tmp_path)
    retrieval = database.tools_by_source["demo"]
    retrieval.full_text_limit = 0

    async def run():
        research_scope = scope()
        discovery = await database.ainvoke(
            DatabaseSearchArguments(source_id="demo"), scope=research_scope
        )
        item = discovery.payload["database_search_result"]["results"][0]
        evidence = Evidence(
            evidence_id="ev-conflicting-work",
            work_id="another-work",
            artifact_id=item["artifact_ids"][0],
            novelty_point_id="NP-1",
            task_id="T-1",
            quote="fixture",
            interpretation="fixture",
            confidence=0.5,
            provenance={
                "artifact_namespace": "research_reference",
                "source_record_id": item["source_record_id"],
            },
        )
        card = EvidenceCard(
            card_id="card-conflict",
            task_id="T-1",
            novelty_point_id="NP-1",
            document_title="fixture",
            main_contribution="fixture",
            relevance=0.5,
            confidence=0.5,
            evidence_ids=[evidence.evidence_id],
        )
        request = NoveltyPointReviewRequest(
            subject_paper_id="subject-1",
            run_id="run-1",
            novelty_point=research_scope.novelty_point,
            tasks=[research_scope.research_task],
            cards=[card],
            evidence=[evidence],
        )
        with pytest.raises(PermissionError, match="manifest binding"):
            await ReviewerDatabaseSearchTool(database).ainvoke(
                DatabaseSearchArguments(
                    source_id="demo",
                    full_text_source_record_ids=[item["source_record_id"]],
                ),
                scope=request,
            )

    asyncio.run(run())
