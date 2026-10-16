"""Reviewer 在 TaskResearcher 主链中的集成契约。"""

from __future__ import annotations

import asyncio
import json

import pytest

from novelty_agent_framework.agents import DemoCoordinator, DemoPointExtractor, DemoSearchPlanner, DemoTaskResearcher
from novelty_agent_framework.ports import ReviewResult, ValidationResult
from novelty_agent_framework.core.report_binding import assemble_report_from_draft
from novelty_agent_framework.schemas import (
    Evidence,
    EvidenceCard,
    EvidenceReviewDecision,
    EvidenceReviewIssue,
    EvidenceSource,
    IssueSeverity,
    NoveltyBrief,
    NoveltyPoint,
    NoveltyPointReview,
    PaperInput,
    ResearchTask,
    ReviewVerdict,
    ReportNarrativeDraft,
)
from novelty_agent_framework.workflows import NoveltyWorkflow, NoveltyWorkflowServices


@pytest.fixture(autouse=True)
def _isolated_outputs(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)


def _paper() -> PaperInput:
    return PaperInput(paper_id="review", title="Review", full_text="body")


def _task() -> ResearchTask:
    return ResearchTask(task_id="T1", novelty_point_id="NP1", task_type="prior", language="en")


def _point() -> NoveltyPoint:
    return NoveltyPoint(point_id="NP1", claim="claim")


def _card(card_id: str) -> EvidenceCard:
    return EvidenceCard(
        card_id=card_id,
        task_id="T1",
        novelty_point_id="NP1",
        document_title="Paper",
        main_contribution="Contribution",
        sources=[EvidenceSource(title="Paper", quote="quote")],
        relevance=0.9,
        confidence=0.8,
    )


class RecordingReviewer:
    def __init__(self, result: ReviewResult | None = None, crash: bool = False):
        self.result = result
        self.crash = crash
        self.cards = ()

    def review(self, cards, *, points, tasks):
        self.cards = tuple(cards)
        if self.crash:
            raise RuntimeError("boom")
        assert self.result is not None
        return self.result


class FilteringValidator:
    def validate(self, cards, *, tasks, **context):
        return ValidationResult(
            accepted=(cards[0],),
            rejected=((cards[1].card_id, "validator_reject"),),
        )


def _workflow(*, reviewer=None, validator=None) -> NoveltyWorkflow:
    return NoveltyWorkflow(
        NoveltyWorkflowServices(
            coordinator=DemoCoordinator(),
            task_researcher=DemoTaskResearcher(),
            search_planner=DemoSearchPlanner(),
            point_extractor=DemoPointExtractor(),
            validator=validator,
            reviewer=reviewer,
        )
    )


def _state(cards):
    return {
        "paper": _paper(),
        "raw_evidence_cards": cards,
        "all_research_tasks": [_task()],
        "novelty_points": [_point()],
        "task_research_results": [],
        "rejected_evidence": [],
    }


def test_validator_rejections_never_reach_reviewer():
    accepted, rejected = _card("accepted"), _card("rejected")
    decision = EvidenceReviewDecision(
        card_id="accepted", verdict=ReviewVerdict.ACCEPT, reviewed_confidence=0.8
    )
    reviewer = RecordingReviewer(
        ReviewResult(accepted=(accepted,), rejected=(), needs_more=(), decisions=(decision,))
    )
    workflow = _workflow(reviewer=reviewer, validator=FilteringValidator())
    validated = asyncio.run(workflow._validate_evidence(_state([accepted, rejected])))
    reviewed = asyncio.run(workflow._review_evidence({**_state([accepted, rejected]), **validated}))
    assert [card.card_id for card in reviewer.cards] == ["accepted"]
    assert reviewed["evidence_cards"] == [accepted]
    assert {item.card_id for item in reviewed["rejected_evidence"]} == {"rejected"}


def test_reject_and_needs_more_do_not_enter_final_evidence():
    rejected, more = _card("reject"), _card("more")
    issue = EvidenceReviewIssue(
        code="unsupported_overlap", message="unsupported", severity=IssueSeverity.WARNING
    )
    decisions = (
        EvidenceReviewDecision(card_id="reject", verdict=ReviewVerdict.REJECT, issues=[issue], reviewed_confidence=0.2),
        EvidenceReviewDecision(card_id="more", verdict=ReviewVerdict.NEEDS_MORE_EVIDENCE, reviewed_confidence=0.4),
    )
    reviewer = RecordingReviewer(
        ReviewResult(accepted=(), rejected=(("reject", "unsupported"),), needs_more=("more",), decisions=decisions)
    )
    state = {**_state([rejected, more]), "validator_accepted_cards": [rejected, more]}
    result = asyncio.run(_workflow(reviewer=reviewer)._review_evidence(state))
    assert result["evidence_cards"] == []
    assert {item.card_id for item in result["rejected_evidence"]} == {"reject"}
    assert {item.code for item in result["issues"]} == {"unsupported_overlap", "needs_more_evidence"}
    persisted = json.loads(open("outputs/review/evidence-cards.json", encoding="utf-8").read())
    assert len(persisted["review_decisions"]) == 2


def test_reviewer_none_transparently_preserves_legacy_format():
    card = _card("accepted")
    result = asyncio.run(
        _workflow()._review_evidence({**_state([card]), "validator_accepted_cards": [card]})
    )
    assert result["evidence_cards"] == [card]
    persisted = json.loads(open("outputs/review/evidence-cards.json", encoding="utf-8").read())
    assert "validator_accepted_cards" not in persisted
    assert "review_decisions" not in persisted
    assert result["issues"][0].code == "reviewer_unavailable"
    assert result["issues"][0].severity == IssueSeverity.ERROR
    assert result["novelty_reviews"][0].incomplete_reason == "technical_error"


def test_reviewer_crash_is_fail_closed_and_audited():
    card = _card("accepted")
    result = asyncio.run(
        _workflow(reviewer=RecordingReviewer(crash=True))._review_evidence(
            {**_state([card]), "validator_accepted_cards": [card]}
        )
    )
    assert result["evidence_cards"] == []
    assert result["rejected_evidence"][0].reason == "review_failed"
    assert result["issues"][0].code == "review_failed"
    assert result["novelty_reviews"][0].incomplete_reason == "technical_error"


@pytest.mark.parametrize("failure_stage", ["point", "card", "summary"])
def test_reviewer_execution_failure_remains_technical_in_report(failure_stage):
    class FailingReviewer:
        async def review(self, request):
            raise RuntimeError("review service unavailable")

    class FailingTwoStageReviewer(FailingReviewer):
        async def review_card(self, request):
            if failure_stage == "card":
                raise RuntimeError("reader unavailable")
            return {"novelty_point_id": "NP1", "status": "insufficient_evidence",
                    "incomplete_reason": "semantic_evidence"}

        async def summarize_reviews(self, request, rows):
            raise RuntimeError("summary service unavailable")

    card = _card("accepted").model_copy(update={"evidence_ids": ["E1"]})
    evidence = Evidence(evidence_id="E1", work_id="W1", artifact_id="A1",
                        novelty_point_id="NP1", task_id="T1", quote="quote",
                        interpretation="partial overlap", confidence=0.8)
    reviewer = FailingReviewer() if failure_stage == "point" else FailingTwoStageReviewer()
    state = {**_state([card]), "validator_accepted_cards": [card], "raw_evidence": [evidence]}
    result = asyncio.run(_workflow(reviewer=reviewer)._review_evidence(state))
    review = result["novelty_reviews"][0]
    assert review.incomplete_reason == "technical_error"
    assert result["evidence_cards"] == [card]
    persisted = json.loads(open("outputs/review/novelty-reviews.json", encoding="utf-8").read())
    assert persisted["reviews"][0]["incomplete_reason"] == "technical_error"
    report = assemble_report_from_draft(
        ReportNarrativeDraft.model_validate({"conclusions": [
            {"novelty_point_id": "NP1", "summary": "untrusted narrative"}
        ]}),
        paper_id="review", novelty_reviews=[review], novelty_points=[_point()],
        evidence_cards=[card],
    )
    conclusion = report.conclusions[0]
    assert conclusion.incomplete_reason == "technical_error"
    assert conclusion.verdict is None
    assert "核验未完成" in conclusion.summary
    assert "关键比较证据不足" not in conclusion.summary


def test_semantic_insufficiency_is_preserved_by_workflow():
    class SemanticReviewer:
        async def review(self, request):
            return NoveltyPointReview(novelty_point_id="NP1", status="insufficient_evidence",
                                      incomplete_reason="semantic_evidence")

    result = asyncio.run(_workflow(reviewer=SemanticReviewer())._review_evidence(_state([])))
    assert result["novelty_reviews"][0].incomplete_reason == "semantic_evidence"
    assert result["issues"] == []


def test_pending_dedup_scope_is_exposed_without_claiming_independent_contributions():
    class PendingExtractor:
        last_trace = {"scope_status": "pending_dedup", "exit_reason": "target_reached"}

        def extract(self, *args, **kwargs):
            return [_point()]

    workflow = _workflow()
    workflow.point_extractor = PendingExtractor()
    result = asyncio.run(workflow._extract_points({"paper": _paper()}))
    issue = next(item for item in result["issues"] if item.code == "point_deduplication_unresolved")
    assert "保留不代表其独立贡献已确认" in issue.message
    assert result["novelty_points"] == [_point()]


def test_scope_limits_survive_report_synthesis():
    from novelty_agent_framework.schemas import WorkflowIssue

    issue = WorkflowIssue(node="extract_points", code="point_deduplication_unresolved",
                          message="保留为待核验范围，不代表独立贡献已确认。")
    state = {"paper": _paper(), "novelty_points": [_point()],
             "brief": NoveltyBrief(paper_summary="summary", novelty_points=[_point()], research_tasks=[]),
             "novelty_reviews": [NoveltyPointReview(novelty_point_id="NP1", status="insufficient_evidence",
                                                    incomplete_reason="material_unavailable")],
             "issues": [issue]}
    report = asyncio.run(_workflow()._synthesize_report(state))["report"]
    assert issue.message in report.limitations
    assert report.conclusions[0].incomplete_reason == "material_unavailable"
