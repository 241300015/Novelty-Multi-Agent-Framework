"""Report Draft is small; all authoritative objects come from trusted input."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from backend.env import ModelCallBudgetExceeded, ModelCallOptions, ModelResponse, ModelTransportTimeout
from novelty_agent_framework.agents import NoveltyCoordinatorAgent
from novelty_agent_framework.core.report_binding import (
    assemble_report_from_draft,
    bind_reviews_to_report,
)
from novelty_agent_framework.schemas import (
    EvidenceCard,
    NoveltyBrief,
    NoveltyPoint,
    NoveltyPointReview,
    PaperInput,
    ReportNarrativeDraft,
    ReviewStatus,
    NoveltyVerdict,
)
from novelty_agent_framework.schemas.domain import ReviewEvidence


def _case(count: int, prefix: str = "P"):
    points = [NoveltyPoint(point_id=f"{prefix}-{i}", claim=f"claim {i}") for i in range(count)]
    cards = [EvidenceCard(
        card_id=f"{prefix}-card-{i}", task_id="T-1", novelty_point_id=point.point_id,
        document_title=f"Doc {i}", main_contribution="mechanism", relevance=0.7,
        confidence=0.8,
    ) for i, point in enumerate(points)]
    reviews = []
    for i, point in enumerate(points):
        status = ReviewStatus.INSUFFICIENT_EVIDENCE if i % 3 == 2 else ReviewStatus.REVIEWED
        review = NoveltyPointReview(
            novelty_point_id=point.point_id, status=status,
            verdict=None if status is ReviewStatus.INSUFFICIENT_EVIDENCE else NoveltyVerdict.NOVEL,
            verdict_reason=None if status is ReviewStatus.INSUFFICIENT_EVIDENCE else "Reviewer reason",
            confidence=None if status is ReviewStatus.INSUFFICIENT_EVIDENCE else 0.72,
            incomplete_reason="technical_error" if status is ReviewStatus.INSUFFICIENT_EVIDENCE else None,
        )
        reviews.append(review)
    draft = ReportNarrativeDraft.model_validate({
        "conclusions": [{
            "novelty_point_id": point.point_id, "summary": f"Short summary {i}",
            "supporting_card_ids": [cards[i].card_id], "counter_card_ids": [],
        } for i, point in reversed(list(enumerate(points)))],
        "limitations": ["Unverified narrative scope"],
    })
    return points, cards, reviews, draft


def test_no_evidence_summary_cannot_claim_absence_of_prior_work():
    points, cards, reviews, draft = _case(3)
    point_id = points[2].point_id
    cards = [c for c in cards if c.novelty_point_id != point_id]
    for narrative in draft.conclusions:
        if narrative.novelty_point_id == point_id:
            narrative.supporting_card_ids = []
            narrative.summary = "已有文献未涉及此技术细节，因此本研究有创新。"
    reviews[2] = reviews[2].model_copy(update={"incomplete_reason": "material_unavailable"})
    report = assemble_report_from_draft(draft, paper_id="paper", novelty_points=points,
        novelty_reviews=reviews, evidence_cards=cards)
    conclusion = report.conclusions[2]
    assert "尚不能作出新颖性裁定" in conclusion.summary
    assert "未涉及" not in conclusion.summary
    assert conclusion.verdict is None
    assert report.conclusions[0].summary == "Short summary 0"


@pytest.mark.parametrize("count", [1, 3, 8])
def test_all_points_order_and_authority_survive_draft_assembly(count):
    points, cards, reviews, draft = _case(count, prefix=f"renamed-{count}")
    if count >= 3:
        quote = " 前导空格\n∑ x² = 1 " * 500
        evidence = ReviewEvidence(
            evidence_id="ev-long", review_id="rv-long", origin_card_id=cards[2].card_id,
            novelty_point_id=points[2].point_id, work_id="work-long", artifact_id="artifact-long",
            namespace="reference", artifact_hash="sha-long", read_id="read-long",
            char_start=0, char_end=len(quote), exact_quote=quote, role="body",
            content_extent="full",
        )
        reviews[2] = reviews[2].model_copy(update={"review_evidence": [evidence]})
    report = assemble_report_from_draft(
        draft, paper_id="paper-renamed", novelty_points=points,
        novelty_reviews=reviews, evidence_cards=cards,
    )
    assert [c.novelty_point_id for c in report.conclusions] == [p.point_id for p in points]
    for conclusion, review in zip(report.conclusions, reviews):
        narrative = next(item for item in draft.conclusions if item.novelty_point_id == conclusion.novelty_point_id)
        if review.incomplete_reason == "technical_error" and not review.review_evidence:
            assert "核验未完成" in conclusion.summary
        else:
            assert conclusion.summary == narrative.summary
        for field in (
            "review_status", "verdict", "verdict_reason", "confidence",
            "highly_relevant_works", "review_evidence", "feature_comparisons", "incomplete_reason",
        ):
            source = "status" if field == "review_status" else field
            assert getattr(conclusion, field) == getattr(review, source)
    if count >= 3:
        assert report.conclusions[2].review_evidence[0].exact_quote == quote
        assert report.conclusions[2].verdict is None
    assert bind_reviews_to_report(
        report, novelty_points=points, novelty_reviews=reviews, evidence_cards=cards,
    ) == report


def test_draft_schema_and_extra_fields_exclude_authority():
    schema = json.dumps(ReportNarrativeDraft.model_json_schema())
    for forbidden in ("review_status", "verdict_reason", "exact_quote", "artifact_hash", "paper_id"):
        assert forbidden not in schema
    points, cards, reviews, draft = _case(1)
    payload = draft.model_dump()
    payload["conclusions"][0]["verdict"] = "novel"
    with pytest.raises(ValidationError, match="Extra inputs"):
        ReportNarrativeDraft.model_validate(payload)


def test_unverified_model_limitations_do_not_enter_authoritative_report():
    points, cards, reviews, draft = _case(1)
    draft = draft.model_copy(update={"limitations": ["检索已覆盖全部中文数据库，零命中。"]})
    report = assemble_report_from_draft(
        draft, paper_id="paper", novelty_points=points,
        novelty_reviews=reviews, evidence_cards=cards,
    )
    assert all("零命中" not in item for item in report.limitations)
    assert "检索覆盖状态未知" in report.limitations[-1]


@pytest.mark.parametrize("mutation,error", [
    ("missing", "missing conclusion"), ("duplicate", "duplicate conclusion"),
    ("unknown", "unknown conclusion"), ("cross_card", "cross-point reference"),
    ("unknown_card", "unknown card reference"), ("conflict", "supporting/counter conflict"),
])
def test_coverage_and_card_scope_fail_closed(mutation, error):
    points, cards, reviews, draft = _case(3)
    data = draft.model_dump()
    conclusions = data["conclusions"]
    if mutation == "missing": conclusions.pop()
    elif mutation == "duplicate": conclusions.append(dict(conclusions[0]))
    elif mutation == "unknown": conclusions[0]["novelty_point_id"] = "foreign"
    elif mutation == "cross_card": conclusions[0]["supporting_card_ids"] = [cards[0].card_id]
    elif mutation == "unknown_card": conclusions[0]["supporting_card_ids"] = ["foreign-card"]
    elif mutation == "conflict": conclusions[0]["counter_card_ids"] = list(conclusions[0]["supporting_card_ids"])
    with pytest.raises(ValueError, match=error):
        assemble_report_from_draft(
            ReportNarrativeDraft.model_validate(data), paper_id="paper",
            novelty_points=points, novelty_reviews=reviews, evidence_cards=cards,
        )


class _Client:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def complete(self, messages, *, options=None):
        self.calls.append((messages, options))
        result = self.responses[len(self.calls) - 1]
        if isinstance(result, Exception):
            raise result
        return result


def _synthesize(client, paper=None):
    points, cards, reviews, draft = _case(1)
    agent = NoveltyCoordinatorAgent(model_client=client, model_options=ModelCallOptions(max_tokens=4096))
    brief = NoveltyBrief(paper_summary="summary", research_problem="problem", novelty_points=points)
    return agent.synthesize(
        paper or PaperInput(paper_id="paper", title="Paper", full_text="body"),
        brief=brief, evidence=cards, novelty_reviews=reviews,
        rejected_evidence=[], insufficient_final_evidence_points=[],
    )


def test_explicit_length_finish_stops_without_retry():
    client = _Client([ModelResponse(
        content='{"conclusions":[]}',
        raw={"choices": [{"finish_reason": "length"}]},
    )])
    with pytest.raises(ValueError, match="output_truncated"):
        _synthesize(client)
    assert len(client.calls) == 1


def test_unterminated_string_stops_as_suspected_truncation():
    client = _Client([ModelResponse(content='{"conclusions":[{"summary":"unfinished')])
    with pytest.raises(ValueError, match="suspected_output_truncation"):
        _synthesize(client)
    assert len(client.calls) == 1


@pytest.mark.parametrize("response,code", [
    (ModelResponse(content=None), "response_unavailable"),
    (ModelTransportTimeout("timeout"), "response_unavailable"),
    (ModelCallBudgetExceeded("cap"), "cap"),
])
def test_missing_response_timeout_and_budget_do_not_retry(response, code):
    client = _Client([response])
    with pytest.raises((ValueError, ModelCallBudgetExceeded), match=code):
        _synthesize(client)
    assert len(client.calls) == 1


def test_complete_bad_draft_gets_one_correction_with_same_schema():
    good = json.dumps({"conclusions": [{
        "novelty_point_id": "P-0", "summary": "short", "supporting_card_ids": ["P-card-0"],
        "counter_card_ids": [],
    }], "limitations": []})
    client = _Client([ModelResponse(content='{"conclusions":[]}'), ModelResponse(content=good)])
    report = _synthesize(client)
    assert report.paper_id == "paper"
    assert len(client.calls) == 2
    assert "ReportNarrativeDraft" in client.calls[1][0][-1].content
    assert "NoveltyReport" not in client.calls[1][0][-1].content


def test_synthesis_prompt_uses_compact_paper_context_without_full_text():
    good = json.dumps({"conclusions": [{
        "novelty_point_id": "P-0", "summary": "short",
        "supporting_card_ids": ["P-card-0"], "counter_card_ids": [],
    }], "limitations": []})
    client = _Client([ModelResponse(content=good)])
    paper = PaperInput(
        paper_id="paper",
        title="Paper title",
        abstract="Trusted abstract",
        full_text="FULL_TEXT_SENTINEL_SHOULD_NOT_REACH_SYNTHESIS",
        references=["REFERENCE_SENTINEL_SHOULD_NOT_REACH_SYNTHESIS"],
    )

    _synthesize(client, paper)

    prompt = client.calls[0][0][-1].content
    assert "Paper title" in prompt and "Trusted abstract" in prompt
    assert "FULL_TEXT_SENTINEL_SHOULD_NOT_REACH_SYNTHESIS" not in prompt
    assert "REFERENCE_SENTINEL_SHOULD_NOT_REACH_SYNTHESIS" not in prompt
