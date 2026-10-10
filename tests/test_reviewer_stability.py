"""Offline production-boundary checks for the frozen S1 summary path."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest

from backend.env import ModelResponse
from novelty_agent_framework.agents import evidence_reviewer as module
from novelty_agent_framework.agents.evidence_reviewer import EvidenceReviewerConfig, NoveltyEvidenceReviewer
from novelty_agent_framework.schemas import NoveltyPointReviewRequest
from scripts.run_reviewer_s1_summary import summary_execution_status


ROOT = Path(__file__).resolve().parents[1]
TRIAL = ROOT / "docs/experiments/20260918_reviewer_contract_finalize/trials/np3_postrepair_pair_20260918"


def frozen_input():
    request = NoveltyPointReviewRequest.model_validate_json((TRIAL / "frozen-input.json").read_text())
    event = json.loads((TRIAL / "L1/outputs/MF2033k6lC/runtime/rv-np3-l1/reviewer_events/0005.json").read_text())
    rows = [{key: row[key] for key in ("index", "card_id", "novelty_point_id", "status", "review")}
            for row in event["rows"]]
    return request, rows


class ScriptedClient:
    def __init__(self, content: str, delay: float = 0):
        self.content, self.delay, self.calls = content, delay, []

    async def acomplete(self, messages, *, options=None):
        self.calls.append((messages, options))
        if self.delay:
            await asyncio.sleep(self.delay)
        return ModelResponse(content=self.content)


def test_summary_does_not_start_first_call_after_assembly_deadline(monkeypatch):
    request, rows = frozen_input()
    client = ScriptedClient("{}")
    original = module._compact_summary_rows

    def slow_assembly(*args):
        result = original(*args)
        time.sleep(0.02)
        return result

    monkeypatch.setattr(module, "_compact_summary_rows", slow_assembly)
    reviewer = NoveltyEvidenceReviewer(client, config=EvidenceReviewerConfig(summary_timeout_seconds=0.01))
    result = asyncio.run(reviewer.summarize_reviews(request, rows))
    assert result.incomplete_reason == "budget_exhausted"
    assert client.calls == []


def test_summary_deadline_cancels_slow_client_without_format_repair():
    request, rows = frozen_input()
    client = ScriptedClient("not json", delay=0.05)
    reviewer = NoveltyEvidenceReviewer(client, config=EvidenceReviewerConfig(summary_timeout_seconds=0.02))
    result = asyncio.run(reviewer.summarize_reviews(request, rows))
    assert result.incomplete_reason == "budget_exhausted"
    assert len(client.calls) == 1
    assert client.calls[0][1].timeout_seconds < 0.02


def test_valid_semantic_insufficiency_is_distinct_from_timeout():
    request, rows = frozen_input()
    client = ScriptedClient(json.dumps({"novelty_point_id": "NP-3",
        "status": "insufficient_evidence", "supplement_request": {"reason": "Further source comparison needed"}}))
    reviewer = NoveltyEvidenceReviewer(client, config=EvidenceReviewerConfig(summary_input_date="2025-01-02"))
    result = asyncio.run(reviewer.summarize_reviews(request, rows))
    assert result.status.value == "insufficient_evidence"
    assert result.incomplete_reason == "semantic_evidence"
    assert json.loads(client.calls[0][0][1].content)["today"] == "2025-01-02"


def test_summary_repair_receives_cross_field_validation_error():
    request, rows = frozen_input()

    class ContractRepairClient:
        def __init__(self):
            self.calls = []

        async def acomplete(self, messages, *, options=None):
            self.calls.append((messages, options))
            draft = {"novelty_point_id": request.novelty_point.point_id,
                     "status": "insufficient_evidence",
                     "supplement_request": {"reason": "Further comparison needed"}}
            if len(self.calls) == 1:
                draft["verdict"] = "partially_novel"
            else:
                payload = json.loads(messages[1].content)
                assert "insufficient_evidence result cannot have verdict" in payload["validation_error"]
                assert options.tool_choice == "none"
                assert not options.tools
            return ModelResponse(content=json.dumps(draft))

    client = ContractRepairClient()
    reviewer = NoveltyEvidenceReviewer(client)
    result = asyncio.run(reviewer.summarize_reviews(request, rows))
    assert len(client.calls) == 2
    assert result.status.value == "insufficient_evidence"
    assert result.verdict is None
    assert result.incomplete_reason == "semantic_evidence"


def test_summary_does_not_buy_format_repair_after_deadline(monkeypatch):
    request, rows = frozen_input()
    client = ScriptedClient("malformed")
    original_extract = module._extract_json

    def slow_parse(content):
        time.sleep(0.08)
        return original_extract(content)

    monkeypatch.setattr(module, "_extract_json", slow_parse)
    reviewer = NoveltyEvidenceReviewer(client, config=EvidenceReviewerConfig(summary_timeout_seconds=0.05))
    result = asyncio.run(reviewer.summarize_reviews(request, rows))
    assert result.incomplete_reason == "budget_exhausted"
    assert len(client.calls) == 1


def test_valid_draft_does_not_complete_after_total_deadline(monkeypatch):
    request, rows = frozen_input()
    content = json.dumps({"novelty_point_id": "NP-3", "status": "insufficient_evidence",
                          "supplement_request": {"reason": "Not enough source context"}})
    client = ScriptedClient(content)
    original_extract = module._extract_json

    def slow_parse(value):
        time.sleep(0.08)
        return original_extract(value)

    monkeypatch.setattr(module, "_extract_json", slow_parse)
    reviewer = NoveltyEvidenceReviewer(client, config=EvidenceReviewerConfig(summary_timeout_seconds=0.05))
    result = asyncio.run(reviewer.summarize_reviews(request, rows))
    assert result.incomplete_reason == "budget_exhausted"
    assert len(client.calls) == 1


def test_s1_script_marks_program_fallback_as_failed_run():
    from novelty_agent_framework.schemas import NoveltyPointReview

    for cause in ("budget_exhausted", "technical_error", "material_unavailable"):
        review = NoveltyPointReview(novelty_point_id="NP-1", status="insufficient_evidence",
                                    incomplete_reason=cause)
        assert summary_execution_status(review) == "FAILED"
    semantic = NoveltyPointReview(novelty_point_id="NP-1", status="insufficient_evidence",
                                  incomplete_reason="semantic_evidence")
    assert summary_execution_status(semantic) == "SUCCESS"


@pytest.mark.parametrize("first", ["", "not json", '{"status":"reviewed"}'])
def test_empty_or_invalid_summary_response_gets_one_bounded_repair(first):
    request, rows = frozen_input()
    repaired = json.dumps({"novelty_point_id": "NP-3", "status": "insufficient_evidence",
                           "supplement_request": {"reason": "Not enough source context"}})

    class TwoResponseClient(ScriptedClient):
        async def acomplete(self, messages, *, options=None):
            self.calls.append((messages, options))
            return ModelResponse(content=[first, repaired][len(self.calls) - 1])

    client = TwoResponseClient(first)
    result = asyncio.run(NoveltyEvidenceReviewer(client).summarize_reviews(request, rows))
    assert result.incomplete_reason == "semantic_evidence"
    assert len(client.calls) == 2
