"""Point-level adoption must survive report binding, gates and rendering."""
from copy import deepcopy
import json

import pytest

from novelty_agent_framework.core.report_binding import assemble_report_from_draft, bind_reviews_to_report
from novelty_agent_framework.core.integrity_gates import validate_report_integrity
from novelty_agent_framework.schemas import NoveltyPointReview, ReportNarrativeDraft
from novelty_agent_framework.tools.renderer import _load_workspace_data
from test_reviewer_summary_handoff import sample
from test_renderer_conclusion_details import seed_multi_workspace, context


def inputs():
    request, _, _ = sample()
    review = NoveltyPointReview(novelty_point_id=request.novelty_point.point_id,
        status="insufficient_evidence", incomplete_reason="semantic_evidence",
        summary_harness_state={"stage": "complete", "adopted_card_ids": [],
                               "discarded_card_ids": [request.cards[0].card_id]})
    return request, review


def assemble(request, review, **groups):
    draft = ReportNarrativeDraft.model_validate({"conclusions": [{
        "novelty_point_id": request.novelty_point.point_id, "summary": "证据不足。", **groups}]})
    return assemble_report_from_draft(draft, paper_id=request.subject_paper_id,
        novelty_points=[request.novelty_point], novelty_reviews=[review], evidence_cards=request.cards)


@pytest.mark.parametrize("group", ["supporting_card_ids", "counter_card_ids"])
def test_discarded_card_cannot_be_grouped_by_coordinator(group):
    request, review = inputs()
    with pytest.raises(ValueError, match="not adopted"):
        assemble(request, review, **{group: [request.cards[0].card_id]})


@pytest.mark.parametrize("group", ["supporting_card_ids", "counter_card_ids"])
def test_compatibility_binding_and_gate_reject_reintroduced_discard(group):
    request, review = inputs()
    report = assemble(request, review)
    report.conclusions[0] = report.conclusions[0].model_copy(
        update={group: [request.cards[0].card_id]})
    with pytest.raises(ValueError, match="not adopted"):
        bind_reviews_to_report(report, novelty_points=[request.novelty_point],
            novelty_reviews=[review], evidence_cards=request.cards)
    gate = validate_report_integrity(report, novelty_points=[request.novelty_point],
        novelty_reviews=[review], evidence_cards=request.cards)
    assert not gate.validation_passed
    assert any("not adopted" in issue for issue in gate.issues)


def test_adoption_is_authoritative_and_legacy_reports_remain_valid():
    request, review = inputs()
    report = assemble(request, review)
    conclusion = report.conclusions[0]
    assert conclusion.summary_harness_state == review.summary_harness_state
    conclusion.summary_harness_state["discarded_card_ids"].clear()
    assert review.summary_harness_state["discarded_card_ids"]
    gate = validate_report_integrity(report, novelty_points=[request.novelty_point],
        novelty_reviews=[review], evidence_cards=request.cards)
    assert not gate.validation_passed
    legacy = review.model_copy(update={"summary_harness_state": {}})
    assert assemble(request, legacy, supporting_card_ids=[request.cards[0].card_id])


@pytest.mark.parametrize("failed", [False, True])
def test_concise_keeps_discard_audit_without_republishing_judgment(tmp_path, failed):
    data = _load_workspace_data(seed_multi_workspace(tmp_path))
    conclusion = data["report"]["conclusions"][0]
    conclusion["summary_harness_state"] = {
        "stage": "select_judgments" if failed else "complete",
        "adopted_card_ids": [] if failed else ["C12"], "discarded_card_ids": ["C11"]}
    for field in ("feature_comparisons", "highly_relevant_works"):
        conclusion[field] = [r for r in conclusion[field] if r["work_id"] != "work-11"]
    discarded = data["reviews"]["card_reviews"][0]["review"]
    discarded["feature_comparisons"][0]["reason"] = "DISCARDED_COMPARISON"
    discarded["support_reason"] = "DISCARDED_SUPPORT"
    before = deepcopy(data)
    detail = context(data)["concise_conclusions"]
    assert "DISCARDED_COMPARISON" not in detail and "DISCARDED_SUPPORT" not in detail
    assert "未采用" in detail and "文献[1]" in detail and "文献[2]" in detail
    assert "已读摘要未说明该机制" in detail
    assert data == before


@pytest.mark.parametrize("discard_all", [False, True])
def test_harness_discard_to_report_gates_and_both_renderers(tmp_path, discard_all):
    from test_reviewer_summary_policy import WireClient, run, two_cards
    from novelty_agent_framework.tools.renderer import _format_conclusions
    from novelty_agent_framework.core.report_adoption import report_review_context

    case = two_cards()  # Historical multi-card input remains readable; not a new creation policy.
    before = deepcopy(case)
    actions = ["放弃这张", "放弃这张", "证据不足"] if discard_all else ["放弃这张", "采用剩余"]
    review, _ = run(WireClient(actions), case=case)
    request = case[0]
    kept = [] if discard_all else ["C-2"]
    assert review.summary_harness_state["adopted_card_ids"] == kept
    report = assemble(request, review, supporting_card_ids=kept)
    # Adoption also survives the compatibility binding and serialization boundary.
    report = type(report).model_validate_json(report.model_dump_json())
    report = bind_reviews_to_report(report, novelty_points=[request.novelty_point],
        novelty_reviews=[review], evidence_cards=request.cards)
    gate = validate_report_integrity(report, novelty_points=[request.novelty_point],
        novelty_reviews=[review], evidence_cards=request.cards)
    assert gate.validation_passed, gate.issues
    assert report.conclusions[0].feature_comparisons == review.feature_comparisons
    assert case == before
    assert len(review.review_evidence) == 14  # Preserve audit ledger, including discarded card.
    projected = report_review_context(review)
    assert {e["origin_card_id"] for e in projected["review_evidence"]} <= set(kept)
    dumped = report.model_dump(mode="json")
    default = _format_conclusions(dumped["conclusions"], {request.novelty_point.point_id: {}})
    assert "未采用单卡：C-1" in default
    assert "rev_ev_0 ·" not in default
    data = _load_workspace_data(seed_multi_workspace(tmp_path))
    data["report"] = dumped
    data["points"]["novelty_points"] = [request.novelty_point.model_dump(mode="json")]
    data["reviews"]["card_reviews"] = case[1]
    data["evidence"]["accepted_evidence_cards"] = [c.model_dump(mode="json") for c in request.cards]
    concise = context(data)["concise_conclusions"]
    assert "本次点级评审未采用" in concise
    if discard_all:
        assert "与该特征相符" not in concise


def test_coordinator_context_and_repair_choices_exclude_discarded_sources():
    from backend.env import ModelResponse
    from novelty_agent_framework.agents import NoveltyCoordinatorAgent
    from novelty_agent_framework.schemas import NoveltyBrief, PaperInput
    from test_report_narrative_contract import _Client
    request, review = inputs()
    good = {"conclusions": [{"novelty_point_id": request.novelty_point.point_id,
                             "summary": "证据不足。"}]}
    bad = deepcopy(good)
    bad["conclusions"][0]["supporting_card_ids"] = [request.cards[0].card_id]
    client = _Client([ModelResponse(content=json.dumps(bad)), ModelResponse(content=json.dumps(good))])
    report = NoveltyCoordinatorAgent(model_client=client).synthesize(
        PaperInput(paper_id=request.subject_paper_id, title="Target", full_text="body"),
        brief=NoveltyBrief(paper_summary="summary", research_problem="problem",
                           novelty_points=[request.novelty_point]),
        evidence=request.cards, novelty_reviews=[review], rejected_evidence=[],
        insufficient_final_evidence_points=[])
    payload = json.loads(client.calls[0][0][1].content.split("输入数据：", 1)[1])
    assert payload["evidence"] == []
    assert "允许的 card ID：[]" in client.calls[1][0][-1].content
    assert report.conclusions[0].supporting_card_ids == []


def test_old_report_uses_same_run_review_adoption_and_default_audit_label(tmp_path):
    from novelty_agent_framework.tools.renderer import _conclusions_with_adoption
    from novelty_agent_framework.tools.renderer_sections import build_stage_context
    data = _load_workspace_data(seed_multi_workspace(tmp_path))
    data["reviews"]["reviews"] = [{"novelty_point_id": "NP-1", "status": "insufficient_evidence",
        "summary_harness_state": {"stage": "complete", "adopted_card_ids": ["C12"],
                                  "discarded_card_ids": ["C11"]}}]
    conclusion = data["report"]["conclusions"][0]
    for field in ("feature_comparisons", "highly_relevant_works"):
        conclusion[field] = [r for r in conclusion[field] if r["work_id"] != "work-11"]
    assert _conclusions_with_adoption(data)[0]["summary_harness_state"]["discarded_card_ids"] == ["C11"]
    assert "本次点级评审未采用" in context(data)["concise_conclusions"]
    sections = build_stage_context(data, tmp_path / "renderer-paper", tmp_path / "report.md")
    assert "本次点级未采用（仅留存审计）" in str(sections)
