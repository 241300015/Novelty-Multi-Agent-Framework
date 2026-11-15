"""Production retains Researcher judgments; only the experiment strips them."""
import asyncio
from contextlib import nullcontext
from copy import deepcopy
import json

import pytest

from scripts.run_reviewer_input_ablation import reviewer_card_model_view, stripped_card_input
from novelty_agent_framework.workflows import reviewer_reading_policy
from test_novelty_point_reviewer import _request
from test_reviewer_controlled_reading import ChoosingClient, reviewer


def test_projection_is_a_whitelist_and_does_not_change_domain_facts():
    request = _request()
    payload = {"novelty_point": request.novelty_point.model_dump(mode="json"),
        "cards": [c.model_dump(mode="json") for c in request.cards],
        "evidence": [e.model_dump(mode="json") for e in request.evidence]}
    payload["cards"][0]["future_researcher_judgment"] = "do not forward"
    payload["evidence"][0]["future_researcher_judgment"] = "do not forward"
    before = deepcopy(payload)
    actual = reviewer_card_model_view(payload)
    assert payload == before
    assert actual["novelty_point"] == before["novelty_point"]
    assert set(actual["cards"][0]) == {"card_id", "task_id", "novelty_point_id", "document_title", "sources", "evidence_ids"}
    assert set(actual["evidence"][0]) == {"evidence_id", "work_id", "artifact_id", "novelty_point_id", "task_id", "quote"}
    for field in actual["cards"][0]:
        assert actual["cards"][0][field] == before["cards"][0][field]
    for field in actual["evidence"][0]:
        assert actual["evidence"][0][field] == before["evidence"][0][field]


def test_default_and_experiment_requests_preserve_their_input_boundary():
    request = _request()
    request.cards[0].main_contribution = "RESEARCHER_CONTRIBUTION"
    request.cards[0].overlaps = ["RESEARCHER_OVERLAP"]
    request.cards[0].differences = ["RESEARCHER_DIFFERENCE"]
    request.evidence[0].interpretation = "RESEARCHER_INTERPRETATION"
    before = request.model_dump(mode="json")
    # A later normal call must not inherit the experiment's temporary filter.
    for stripped in (False, True, False):
        client = ChoosingClient()
        with stripped_card_input() if stripped else nullcontext():
            result = asyncio.run(reviewer(client).review_card(request))
        assert result.status.value == "reviewed"
        assert result.review_evidence and result.reader_observations
        assert request.model_dump(mode="json") == before
        for messages, _, _ in client.calls:
            initial = json.loads(messages[1].content)
            assert initial["evidence"][0]["quote"] == request.evidence[0].quote
            assert "work_choice" in initial["evidence"][0]
            assert "evidence_choices" in initial["cards"][0]
            if stripped:
                assert "RESEARCHER_" not in "\n".join(m.content or "" for m in messages)
                assert "confidence" not in initial["evidence"][0]
                assert "relevance" not in initial["cards"][0]
            else:
                for field in ("main_contribution", "overlaps", "differences", "cited_by_paper",
                              "possible_baseline", "relevance", "confidence"):
                    assert initial["cards"][0][field] == before["cards"][0][field]
                for field in ("interpretation", "confidence"):
                    assert initial["evidence"][0][field] == before["evidence"][0][field]


def test_experiment_filter_is_removed_after_failure():
    original = reviewer_reading_policy.ReviewerChoices
    with pytest.raises(RuntimeError, match="experiment interrupted"):
        with stripped_card_input():
            assert reviewer_reading_policy.ReviewerChoices is not original
            raise RuntimeError("experiment interrupted")
    assert reviewer_reading_policy.ReviewerChoices is original
