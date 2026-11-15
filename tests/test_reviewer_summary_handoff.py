"""Point synthesis inherits judgments; retained reading blocks are not its input."""
import copy
import json

import pytest

from novelty_agent_framework.agents.evidence_reviewer import (
    _compact_summary_rows,
    _summary_model_rows, _verify_summary_payload,
)
from novelty_agent_framework.schemas.domain import ReviewerSupportReview, ReviewEvidence
from novelty_agent_framework.schemas.reviewer_handoff import CardSummaryHandoff
from test_novelty_point_reviewer import _request


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


def test_failed_card_has_no_selected_evidence_or_judgment():
    request, rows, _ = sample()
    rows[0].update(status="failed", error="material unavailable")
    compact, ids = _compact_summary_rows(request, rows)
    handoff = _summary_model_rows(compact)[0]
    assert not ids and not handoff["selected_evidence"] and handoff["review"] is None


def test_incomplete_inner_review_is_not_a_completed_handoff():
    request, rows, _ = sample()
    rows[0]["review"].update(status="insufficient_evidence", incomplete_reason="budget_exhausted")
    compact, ids = _compact_summary_rows(request, rows)
    assert not ids
    assert _summary_model_rows(compact)[0]["status"] == "failed"
    assert _summary_model_rows(compact)[0]["review"] is None
