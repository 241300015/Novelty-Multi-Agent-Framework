"""Point synthesis inherits judgments; retained reading blocks are not its input."""
import asyncio
import copy
import json

import pytest

from backend.env import ModelResponse
from novelty_agent_framework.agents.evidence_reviewer import (
    NoveltyEvidenceReviewer, EvidenceReviewerConfig, _compact_summary_rows,
    _summary_model_rows, _verify_summary_payload,
)
from novelty_agent_framework.schemas.domain import ReviewerSupportReview, ReviewEvidence
from novelty_agent_framework.schemas.reviewer_handoff import CardSummaryHandoff
from test_novelty_point_reviewer import _request, ScriptedClient


def sample():
    request = _request()
    request.novelty_point.technical_features = ["leader-worker", "edge partition", "summary mini-batch"]
    # NP-2 shape: seven 8,000-character blocks, six selected, one only read.
    evidence = [ReviewEvidence(evidence_id=f"rev_ev_{i}", review_id="review-1", origin_card_id="C-1",
        novelty_point_id="NP-1", work_id="work-1", artifact_id="artifact-1",
        namespace="research_reference", artifact_hash="hash", read_id=f"read-{i}",
        char_start=i*8000, char_end=(i+1)*8000, exact_quote=f"SOURCE_{i}_".ljust(8000, "x"),
        role="extracted_text", content_extent="partial") for i in range(7)]
    comparisons = [{"feature_id": f"F{i+1}", "work_id": "work-1", "relation": "supported",
        "basis_type": "direct_statement", "source_context": "author_method",
        "evidence_refs": [f"rev_ev_{i*2}", f"rev_ev_{i*2+1}"], "reason": f"card judgment {i+1}"}
        for i in range(3)]
    review = ReviewerSupportReview(novelty_point_id="NP-1", status="reviewed", support_status="usable",
        support_reason="selected card judgments", harness_state={"stage": "complete"},
        highly_relevant_works=[{"work_id": "work-1", "card_ids": ["C-1"],
            "evidence_ids": [f"rev_ev_{i}" for i in range(6)], "relevance_reason": "related"}],
        feature_comparisons=comparisons, review_evidence=evidence)
    rows = [{"index": 1, "card_id": "C-1", "novelty_point_id": "NP-1", "status": "completed",
             "review": review.model_dump(mode="json")}]
    final = {"novelty_point_id": "NP-1", "status": "insufficient_evidence",
        "highly_relevant_works": review.model_dump(mode="json")["highly_relevant_works"],
        "feature_comparisons": comparisons,
        "supplement_request": {"reason": "retrieval coverage requires further assessment"}}
    return request, rows, final


def run(request, rows, outputs, **config):
    client = ScriptedClient(*(ModelResponse(content=json.dumps(o)) for o in outputs))
    client.summary_fixture = (request, rows)
    reviewer = NoveltyEvidenceReviewer(client, config=EvidenceReviewerConfig(**config))
    result = asyncio.run(reviewer.summarize_reviews(request, rows))
    return result, client


def recheck(eids=None, **updates):
    target = {"card_id": "C-1", "work_id": "work-1", "feature_id": "F3",
              "evidence_ids": eids or ["rev_ev_4", "rev_ev_5"],
              "question": "Does model averaging establish graph-summary mini-batch training?"}
    target.update(updates)
    return {"recheck": target}


def test_np2_shape_inherits_six_long_evidence_handles_without_reading_or_truncation():
    request, rows, final = sample()
    before = copy.deepcopy(rows)
    result, client = run(request, rows, [final])
    assert result.incomplete_reason == "semantic_evidence", result
    assert result.feature_comparisons[-1].evidence_refs == ["rev_ev_4", "rev_ev_5"]
    assert len(client.calls) == 1
    payload = json.loads(client.calls[0][0][1].content)
    handoff = payload["card_reviews"][0]
    assert handoff["contract_version"] == "card_choices_v2"
    assert len(handoff["selected_evidence"]) == 6
    wire = json.dumps(payload)
    for forbidden in ["SOURCE_", "exact_quote", "review_evidence", "reader_observations", "key_quotes", "rev_ev_6"]:
        assert forbidden not in wire
    assert len(result.review_evidence) == 7  # Audit records are retained, not silently deleted.
    assert rows == before


def test_handoff_size_is_independent_of_reading_block_size():
    request, rows, _ = sample()
    compact, ids = _compact_summary_rows(request, rows)
    baseline = _summary_model_rows(compact)
    for item in rows[0]["review"]["review_evidence"]:
        item["exact_quote"] *= 10
        item["char_end"] = item["char_start"] + len(item["exact_quote"])
    larger, more_ids = _compact_summary_rows(request, rows)
    assert _summary_model_rows(larger) == baseline
    assert ids == more_ids == {f"rev_ev_{i}" for i in range(6)}
    with pytest.raises(ValueError):
        CardSummaryHandoff.model_validate({**baseline[0], "review_evidence": []})
    tampered = copy.deepcopy(baseline)
    tampered[0]["review"]["feature_comparisons"][0]["relation"] = "contradicted"
    with pytest.raises(ValueError, match="handoff"):
        _verify_summary_payload(compact, json.dumps({"card_reviews": tampered}))


def test_directed_recheck_only_exposes_requested_sources_and_allows_changed_judgment():
    request, rows, final = sample()
    final["feature_comparisons"][2].update(relation="unknown", basis_type="insufficient",
        reason="Model averaging alone does not establish the target mechanism")
    result, client = run(request, rows, [recheck(), final])
    assert result.incomplete_reason == "semantic_evidence", result
    assert len(client.calls) == 2
    assert "SOURCE_" not in client.calls[0][0][1].content
    visible = json.loads(client.calls[1][0][-1].content)["targeted_verification"]
    assert len({q["evidence_choice"] for q in visible["quotes"]}) == 2
    assert all("evidence_id" not in q for q in visible["quotes"])
    assert visible["source"] == "retained_card_evidence" and not visible["new_reader_call"]
    assert all(len(q["quote"]) == 8000 for q in visible["quotes"])


@pytest.mark.parametrize("updates", [{"work_id": "wrong"}, {"card_id": "wrong"},
    {"feature_id": "F99"}, {"evidence_ids": ["rev_ev_6"]}, {"evidence_ids": ["invented"]}])
def test_recheck_rejects_unselected_or_wrong_scope(updates):
    request, rows, final = sample()
    result, client = run(request, rows, [recheck(**updates), final])
    assert result.incomplete_reason == "technical_error"
    assert len(client.calls) == 1


def test_changed_feature_cannot_borrow_an_inherited_citation_without_recheck():
    request, rows, final = sample()
    final["feature_comparisons"][2]["relation"] = "contradicted"
    result, client = run(request, rows, [final])
    assert result.incomplete_reason == "technical_error"
    assert "targeted verification" in result.supplement_request.reason
    assert len(client.calls) == 1


def test_verification_of_one_feature_does_not_authorize_changes_to_another():
    request, rows, final = sample()
    final["feature_comparisons"][0].update(relation="contradicted", evidence_refs=["rev_ev_4"])
    result, _ = run(request, rows, [recheck(), final])
    assert result.incomplete_reason == "technical_error"


def test_read_only_ledger_entry_is_not_citable_in_final():
    request, rows, final = sample()
    final["highly_relevant_works"][0]["evidence_ids"].append("rev_ev_6")
    result, _ = run(request, rows, [final])
    assert result.incomplete_reason == "technical_error"


@pytest.mark.parametrize("config", [{"summary_max_recheck_chars": 7999}, {"summary_max_calls": 1}])
def test_recheck_budget_exhaustion_never_authorizes_truncated_source(config):
    request, rows, final = sample()
    result, client = run(request, rows, [recheck(), final], **config)
    assert result.incomplete_reason == "budget_exhausted"
    assert len(client.calls) == 1


def test_legacy_card_without_feature_mapping_can_only_inherit_what_it_provided():
    request, rows, final = sample()
    legacy = {**final, "feature_comparisons": [], "review_evidence": rows[0]["review"]["review_evidence"]}
    rows[0]["review"] = legacy
    result, _ = run(request, rows, [final])
    assert result.incomplete_reason == "technical_error"
    final["feature_comparisons"] = []
    result, _ = run(request, rows, [final])
    assert result.incomplete_reason == "semantic_evidence"


def test_failed_card_has_no_selected_evidence_or_judgment():
    request, rows, _ = sample()
    rows[0].update(status="failed", error="material unavailable")
    compact, ids = _compact_summary_rows(request, rows)
    handoff = _summary_model_rows(compact)[0]
    assert not ids and not handoff["selected_evidence"] and handoff["review"] is None


def test_repair_keeps_default_handoff_and_does_not_expose_reading_ledger():
    request, rows, final = sample()
    result, client = run(request, rows, [{"status": "reviewed"}, final])
    assert result.incomplete_reason == "semantic_evidence"
    assert len(client.calls) == 2
    assert all("SOURCE_" not in m.content for messages, _ in client.calls for m in messages)


def test_unknown_is_inherited_without_reading_and_no_new_point_verdict_policy():
    request, rows, final = sample()
    unknown = {**final["feature_comparisons"][2], "relation": "unknown", "basis_type": "insufficient",
               "evidence_refs": [], "reason": "not established by the card"}
    rows[0]["review"]["feature_comparisons"][2] = unknown
    final["feature_comparisons"][2] = unknown
    result, client = run(request, rows, [final])
    assert result.incomplete_reason == "semantic_evidence"
    assert result.feature_comparisons[2].relation == "unknown"
    assert len(client.calls) == 1


def test_incomplete_inner_review_is_not_a_completed_handoff():
    request, rows, _ = sample()
    rows[0]["review"].update(status="insufficient_evidence", incomplete_reason="budget_exhausted")
    compact, ids = _compact_summary_rows(request, rows)
    assert not ids
    assert _summary_model_rows(compact)[0]["status"] == "failed"
    assert _summary_model_rows(compact)[0]["review"] is None


def test_recheck_authorization_does_not_leak_into_the_next_summary_invocation():
    request, rows, final = sample()
    final["feature_comparisons"][2]["relation"] = "contradicted"
    outputs = [recheck(), final, final]
    client = ScriptedClient(*(ModelResponse(content=json.dumps(o)) for o in outputs))
    client.summary_fixture = (request, rows)
    reviewer = NoveltyEvidenceReviewer(client)
    first = asyncio.run(reviewer.summarize_reviews(request, rows))
    second = asyncio.run(reviewer.summarize_reviews(request, rows))
    assert first.incomplete_reason == "semantic_evidence"
    assert second.incomplete_reason == "technical_error"


def test_conflicting_cards_require_recheck_even_when_one_matches_final():
    request, rows, final = sample()
    request.cards.append(request.cards[0].model_copy(update={"card_id": "C-2"}))
    other = copy.deepcopy(rows[0])
    other.update(index=2, card_id="C-2")
    other["review"]["highly_relevant_works"][0]["card_ids"] = ["C-2"]
    for item in other["review"]["review_evidence"]:
        item["origin_card_id"] = "C-2"
        item["evidence_id"] += "_other"
    for work in other["review"]["highly_relevant_works"]:
        work["evidence_ids"] = [eid + "_other" for eid in work["evidence_ids"]]
    for comparison in other["review"]["feature_comparisons"]:
        comparison["evidence_refs"] = [eid + "_other" for eid in comparison["evidence_refs"]]
    other["review"]["feature_comparisons"][2]["relation"] = "contradicted"
    rows.append(other)
    result, _ = run(request, rows, [final])
    assert result.incomplete_reason == "technical_error"
    result, _ = run(request, rows, [recheck(), final])
    assert result.incomplete_reason == "semantic_evidence", result


def test_work_cannot_append_selected_evidence_from_an_unlisted_card():
    request, rows, final = sample()
    # Both cards concern the same Work, but E-2 belongs only to C-2.
    request.evidence.append(request.evidence[0].model_copy(update={"evidence_id": "E-2"}))
    request.cards.append(request.cards[0].model_copy(update={"card_id": "C-2", "evidence_ids": ["E-2"]}))
    rows.append({"index": 2, "card_id": "C-2", "novelty_point_id": "NP-1", "status": "completed",
        "review": {"novelty_point_id": "NP-1", "status": "insufficient_evidence",
            "highly_relevant_works": [{"work_id": "work-1", "card_ids": ["C-2"],
                "evidence_ids": ["E-2"], "relevance_reason": "second card"}]}})
    final["highly_relevant_works"][0]["evidence_ids"].append("E-2")
    result, _ = run(request, rows, [final])
    assert result.incomplete_reason == "technical_error"
    assert "unlisted card" in result.supplement_request.reason
