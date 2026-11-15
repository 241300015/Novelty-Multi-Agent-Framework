"""Prompt delivery through the real Harness; mocks do not test model semantics."""
import asyncio
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest
from backend.env import PromptLibrary
from novelty_agent_framework.agents.evidence_reviewer import (
    NoveltyEvidenceReviewer, _SUMMARY_FALLBACK, _SUMMARY_CONTRACT,
)
from novelty_agent_framework.agents.reviewer_card import SUPPORT_INSTRUCTION, SUPPORT_CHOICE_CONTRACT
from test_novelty_point_reviewer import _request
from test_reviewer_controlled_reading import ChoosingClient, reviewer
from test_reviewer_summary_handoff import sample
from test_reviewer_summary_policy import WireClient


PROMPTS = Path(__file__).resolve().parents[1] / "backend/src/novelty_agent_framework/prompts"


def configure(service, mode):
    if mode == "library":
        service._prompts = PromptLibrary(PROMPTS)
    elif mode == "custom":
        service._prompts = SimpleNamespace(render=lambda *a, **kw: SimpleNamespace(system="custom deployment"))


@pytest.mark.parametrize("name,fallback,version", [
    ("review_support", SUPPORT_INSTRUCTION, "4"),
    ("summarize_reviews", _SUMMARY_FALLBACK, "5"),
])
def test_versioned_prompt_and_no_library_fallback_have_identical_instructions(name, fallback, version):
    prompt = PromptLibrary(PROMPTS).render("reviewer/" + name)
    assert prompt.version == version
    assert prompt.system == fallback
    assert prompt.user == ""


@pytest.mark.parametrize("mode", ["library", "fallback", "custom"])
@pytest.mark.parametrize("read", [False, True])
def test_single_card_delivers_adapted_prompt_without_changing_stage_contract(mode, read):
    client = ChoosingClient(read=read)
    service = reviewer(client)
    configure(service, mode)
    request = _request()
    request.novelty_point.technical_features = ["固定规模映射", "重构误差训练"]
    result = asyncio.run(service.review_card(request))
    assert result.status.value == "reviewed"
    expected = "custom deployment" if mode == "custom" else SUPPORT_INSTRUCTION
    for messages, options, context in client.calls:
        assert messages[0].content.startswith(expected + SUPPORT_CHOICE_CONTRACT)
        properties = options.response_format["json_schema"]["schema"]["properties"]
        if context["state"]["stage"] != "draft":
            assert set(properties) == {"option_id"}
        else:
            assert set(properties) == {"material_assessment", "material_evidence_choices",
                                       "feature_judgments", "supplement_request"}
            assert len(properties["feature_judgments"]["properties"]) == 2
    assert bool(result.review_evidence) == read


@pytest.mark.parametrize("mode", ["library", "fallback", "custom"])
def test_summary_delivers_adapted_prompt_and_discard_does_not_rewrite_card(mode):
    request, rows, _ = sample()
    original = deepcopy(rows)
    client = WireClient(actions=["定向复核", "放弃这张单卡", "按证据不足"])
    service = NoveltyEvidenceReviewer(client)
    configure(service, mode)
    result = asyncio.run(service.summarize_reviews(request, rows))
    assert result.summary_harness_state["stage"] == "complete"
    assert result.summary_harness_state["discarded_card_ids"] == ["C-1"]
    assert result.status.value == "insufficient_evidence"
    assert not result.feature_comparisons
    assert rows == original
    expected = "custom deployment" if mode == "custom" else _SUMMARY_FALLBACK
    for messages, _ in client.calls:
        assert messages[0].content.startswith(expected + _SUMMARY_CONTRACT)
    assert set(client.calls[-1][1].response_format["json_schema"]["schema"]["properties"]) == {
        "status", "verdict", "verdict_reason", "confidence", "supplement_request"}
