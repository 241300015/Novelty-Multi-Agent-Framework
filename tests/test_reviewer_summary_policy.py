"""Actual point Harness wire: choices in, unchanged card facts out. No fixture translation."""
import asyncio
from copy import deepcopy
import json

import pytest
from backend.env import ModelResponse, ModelToolCall
from novelty_agent_framework.agents.evidence_reviewer import NoveltyEvidenceReviewer, EvidenceReviewerConfig
from novelty_agent_framework.schemas import NoveltyPointReview
from test_reviewer_summary_handoff import sample
from test_reviewer_strict_schema import assert_strict_objects


def narrative():
    return {"status": "insufficient_evidence", "verdict": None, "verdict_reason": None,
            "confidence": None, "supplement_request": {"reason": "检索覆盖仍有限",
            "missing_aspects": [], "suggested_focus": []}}


class WireClient:
    def __init__(self, actions=(), final=None):
        self.actions, self.final = list(actions), final
        self.calls, self.final_calls = [], 0

    async def acomplete(self, messages, *, options=None):
        self.calls.append((list(messages), options))
        repair = messages[0].content.startswith("Repair the format")
        if not repair:
            self.payload = json.loads(messages[1].content)
            self.context = next(json.loads(m.content) for m in reversed(messages)
                                if m.content.startswith('{"summary_state":'))
            menu = json.loads(messages[-1].content.split("\n")[1])
            if menu:
                action = self.actions.pop(0) if self.actions else next(iter(menu.values()))
                if callable(action):
                    return action(menu, self)
                key = next(key for key, label in menu.items() if action in label)
                return ModelResponse(content=json.dumps({"option_id": key}))
        self.final_calls += 1
        output = self.final(self, repair) if self.final else narrative()
        return ModelResponse(content=output if isinstance(output, str) else json.dumps(output))


def run(client=None, *, case=None, **config):
    request, rows, _ = case or sample()
    client = client or WireClient()
    result = asyncio.run(NoveltyEvidenceReviewer(client, config=EvidenceReviewerConfig(**config))
                         .summarize_reviews(request, rows))
    return result, client


def two_cards(*, conflict=False):
    request, rows, final = sample()
    request.cards.append(request.cards[0].model_copy(update={"card_id": "C-2"}))
    other = deepcopy(rows[0])
    other.update(index=2, card_id="C-2")
    other["review"]["highly_relevant_works"][0]["card_ids"] = ["C-2"]
    for e in other["review"]["review_evidence"]:
        e.update(origin_card_id="C-2", evidence_id=e["evidence_id"] + "_other")
    for work in other["review"]["highly_relevant_works"]:
        work["evidence_ids"] = [e + "_other" for e in work["evidence_ids"]]
    for comparison in other["review"]["feature_comparisons"]:
        comparison["evidence_refs"] = [e + "_other" for e in comparison["evidence_refs"]]
    if conflict:
        other["review"]["feature_comparisons"][2]["relation"] = "contradicted"
    rows.append(other)
    return request, rows, final


def test_default_adoption_copies_all_card_facts_exactly_without_sources_or_model_ids():
    case = sample()
    before = deepcopy(case)
    result, client = run(case=case)
    assert result.incomplete_reason == "semantic_evidence", result
    assert result.summary_harness_state["stage"] == "complete"
    assert result.summary_harness_state["adopted_card_ids"] == ["C-1"]
    for field in ("feature_comparisons", "highly_relevant_works"):
        assert result.model_dump(mode="json")[field] == case[1][0]["review"][field]
    assert len(client.calls) == 2 and case == before
    for messages, options in client.calls:
        source = "\n".join(m.content for m in messages)
        for forbidden in ('"evidence_id"', '"evidence_refs"', '"work_id"', '"card_id"', 'SOURCE_', 'rev_ev_'):
            assert forbidden not in source
        assert options.tools == () and options.tool_choice == "none"
        assert_strict_objects(options.response_format["json_schema"]["schema"])
    assert set(client.calls[0][1].response_format["json_schema"]["schema"]["properties"]) == {"option_id"}
    assert set(client.calls[1][1].response_format["json_schema"]["schema"]["properties"]) == set(narrative())
    assert len(result.review_evidence) == 7


@pytest.mark.parametrize("field", ["feature_comparisons", "highly_relevant_works", "revised_comparisons",
                                  "additional_works", "inherit_comparisons", "novelty_point_id", "recheck"])
def test_final_and_repair_never_accept_judgment_or_identity_fields(field):
    result, client = run(WireClient(final=lambda c, r: {**narrative(), field: []}))
    assert result.incomplete_reason == "technical_error"
    assert not result.feature_comparisons
    assert len(client.calls) == 3
    assert client.calls[-1][1].response_format == client.calls[-2][1].response_format
    assert field not in json.loads(client.calls[-1][0][1].content)["schema"]["properties"]


def test_format_repair_repairs_narrative_only_and_preserves_adopted_facts():
    result, client = run(WireClient(final=lambda c, repair: narrative() if repair else "{invalid"))
    assert result.incomplete_reason == "semantic_evidence"
    assert len(result.feature_comparisons) == 3
    assert len(client.calls) == 3
    assert "SOURCE_" not in str(client.calls)


def test_targeted_verification_then_discard_whole_card_preserves_other_cards_and_audit():
    case = two_cards()
    before = deepcopy(case)
    client = WireClient(["/ F3", "放弃这张", "采用剩余"])
    result, client = run(client, case=case)
    assert result.incomplete_reason == "semantic_evidence", result
    assert result.summary_harness_state["discarded_card_ids"] == ["C-1"]
    assert result.summary_harness_state["adopted_card_ids"] == ["C-2"]
    assert result.model_dump(mode="json")["feature_comparisons"] == case[1][1]["review"]["feature_comparisons"]
    assert result.highly_relevant_works[0].card_ids == ["C-2"]
    assert len(result.review_evidence) == 14 and case == before
    visible = "\n".join(m.content for m in client.calls[1][0])
    assert "SOURCE_4_" in visible and "SOURCE_5_" in visible
    assert "SOURCE_0_" not in visible and "SOURCE_6_" not in visible
    assert '"new_reader_call": false' in visible and '"evidence_id"' not in visible
    assert len(client.calls) == 4


def test_recheck_then_keep_cannot_rewrite_any_judgment():
    result, _ = run(WireClient(["/ F3", "保留这张", "采用剩余"]))
    assert result.model_dump(mode="json")["feature_comparisons"] == sample()[1][0]["review"]["feature_comparisons"]
    assert result.summary_harness_state["discarded_card_ids"] == []


def test_all_discarded_forces_insufficiency_and_empty_comparisons_without_erasing_audit():
    result, client = run(WireClient(["放弃这张", "证据不足"]))
    assert result.incomplete_reason == "semantic_evidence"
    assert not result.feature_comparisons and not result.highly_relevant_works
    assert len(result.review_evidence) == 7
    assert client.calls[-1][1].response_format["json_schema"]["schema"]["properties"]["status"]["enum"] == ["insufficient_evidence"]
    result, _ = run(WireClient(["放弃这张", "证据不足"], final=lambda c, r:
        {**narrative(), "status": "reviewed", "verdict": "novel", "verdict_reason": "unsupported", "confidence": .9}))
    assert result.incomplete_reason == "technical_error" and result.verdict is None


def test_conflicting_cards_cannot_be_adopted_for_a_verdict_until_a_card_is_discarded():
    case = two_cards(conflict=True)
    result, client = run(case=case)
    assert result.incomplete_reason == "semantic_evidence"
    assert len(result.feature_comparisons) == 6  # Conflict remains visible, not silently merged.
    assert client.context["summary_state"]["conflicts"]
    assert result.summary_harness_state["force_insufficient"]
    result, _ = run(WireClient(["放弃这张", "作出点级裁定"], final=lambda c, r: {
        "status": "reviewed", "verdict": "partially_novel", "verdict_reason": "有限比较",
        "confidence": .5, "supplement_request": None}), case=case)
    assert result.summary_harness_state["adopted_card_ids"] == ["C-2"]
    assert not result.summary_harness_state["force_insufficient"]


def test_recheck_budget_hides_oversized_target_and_never_truncates():
    result, client = run(summary_max_recheck_chars=7999)
    assert result.incomplete_reason == "semantic_evidence"
    assert "定向复核" not in client.calls[0][0][-1].content
    assert "SOURCE_" not in str(client.calls)
    result, _ = run(WireClient(["/ F3", "保留这张", "采用剩余"]), summary_max_calls=2)
    assert result.incomplete_reason == "budget_exhausted"
    assert result.summary_harness_state["stage"] == "select_judgments"


@pytest.mark.parametrize("bad", ["early_final", "stale", "native", "extra_arguments"])
def test_menu_rejects_early_finish_stale_and_model_authored_tool_arguments(bad):
    def invalid(menu, client):
        if bad == "native":
            return ModelResponse(content=None, tool_calls=(ModelToolCall(id="bad", name="reviewer_recheck",
                arguments={"card_id": "invented"}),))
        value = narrative() if bad == "early_final" else {"option_id": "rs_previous_invocation_1"}
        if bad == "extra_arguments":
            value = {"option_id": next(iter(menu)), "evidence_ids": ["invented"]}
        return ModelResponse(content=json.dumps(value))
    result, client = run(WireClient([invalid]))
    assert result.incomplete_reason == "semantic_evidence"
    assert len(client.calls) == 3
    assert [d["action"] for d in result.summary_harness_state["decisions"]] == ["insufficient"]


def test_unknown_empty_refs_and_old_cards_remain_unchanged():
    case = sample()
    case[1][0]["review"]["feature_comparisons"][2].update(
        relation="unknown", basis_type="insufficient", evidence_refs=[], reason="无法确认")
    result, client = run(case=case)
    assert result.feature_comparisons[2].relation == "unknown"
    assert result.feature_comparisons[2].evidence_refs == []
    assert "/ F3" not in client.calls[0][0][-1].content
    case[1][0]["review"] = {"novelty_point_id": "NP-1", "status": "insufficient_evidence"}
    result, _ = run(case=case)
    assert result.incomplete_reason == "semantic_evidence" and not result.feature_comparisons
    old = NoveltyPointReview(novelty_point_id="NP-1", status="insufficient_evidence")
    assert old.summary_harness_state == {}


def test_failure_after_discard_preserves_choice_audit_and_next_invocation_starts_fresh():
    result, _ = run(WireClient(["放弃这张"]), summary_max_calls=1)
    assert result.incomplete_reason == "budget_exhausted"
    assert result.summary_harness_state["discarded_card_ids"] == ["C-1"]
    result, _ = run()
    assert result.summary_harness_state["adopted_card_ids"] == ["C-1"]


def test_previous_menu_option_cannot_drop_a_card_in_a_new_invocation():
    saved = []
    def remember(menu, client):
        saved.append(next(key for key, label in menu.items() if "放弃这张" in label))
        return ModelResponse(content=json.dumps({"option_id": next(iter(menu))}))
    first, _ = run(WireClient([remember]))
    second, client = run(WireClient([lambda menu, c: ModelResponse(content=json.dumps({"option_id": saved[0]}))]))
    assert first.summary_harness_state["adopted_card_ids"] == second.summary_harness_state["adopted_card_ids"] == ["C-1"]
    assert len(client.calls) == 3 and second.summary_harness_state["discarded_card_ids"] == []


def test_failed_recheck_can_discard_without_claiming_a_successful_verification(monkeypatch):
    from novelty_agent_framework.core.reviewer_summary import SummaryVerification
    def fail(self, target):
        raise ValueError("retained evidence unavailable")
    monkeypatch.setattr(SummaryVerification, "recheck", fail)
    result, client = run(WireClient(["/ F3", "放弃这张", "证据不足"]))
    assert result.incomplete_reason == "semantic_evidence"
    assert result.summary_harness_state["rechecks"][0]["succeeded"] is False
    assert not result.feature_comparisons
    assert "本次复核未完成" in str(client.calls)


@pytest.mark.parametrize("updates", [{"work_id": "wrong"}, {"card_id": "wrong"},
    {"feature_id": "F99"}, {"evidence_ids": ["rev_ev_6"]}, {"evidence_ids": ["invented"]}])
def test_internal_verification_still_checks_scopes_and_rejects_unselected_reading_records(updates):
    from novelty_agent_framework.agents.evidence_reviewer import _compact_summary_rows
    from novelty_agent_framework.core.reviewer_summary import SummaryVerification
    from novelty_agent_framework.schemas.reviewer_handoff import SummaryRecheckTarget
    request, rows, _ = sample()
    verification = SummaryVerification(request, _compact_summary_rows(request, rows)[0],
                                       max_chars=32000, max_requests=2)
    target = SummaryRecheckTarget.model_validate({"card_id": "C-1", "work_id": "work-1", "feature_id": "F3",
        "evidence_ids": ["rev_ev_4", "rev_ev_5"], "question": "check", **updates})
    with pytest.raises(ValueError):
        verification.recheck(target)
    assert verification.records == []


def test_workflow_uses_harness_adoption_and_persists_discard_without_changing_card_results(tmp_path, monkeypatch):
    from test_novelty_point_reviewer import _workflow
    from novelty_agent_framework.schemas import PaperInput
    from novelty_agent_framework.schemas.domain import ReviewerSupportReview
    monkeypatch.chdir(tmp_path)
    request, rows, _ = two_cards()
    before = deepcopy(rows)
    client = WireClient(["放弃这张", "采用剩余"])
    service = NoveltyEvidenceReviewer(client)

    class Reviewer:
        # This integration isolates orchestration from artifact checkpoint I/O,
        # which is exercised separately by test_reviewer_summary_checkpoint.
        async def review(self, request):
            raise AssertionError("two-stage workflow must not use legacy point review")

        async def review_card(self, request):
            cid = request.cards[0].card_id
            return ReviewerSupportReview.model_validate(next(r["review"] for r in rows if r["card_id"] == cid))

        async def summarize_reviews(self, request, cards):
            return await service.summarize_reviews(request, cards)

    result = asyncio.run(_workflow(Reviewer())._review_evidence({
        "paper": PaperInput(paper_id="paper-1", title="Paper", full_text="body"),
        "novelty_points": [request.novelty_point], "all_research_tasks": request.tasks,
        "raw_evidence": request.evidence, "validator_accepted_cards": request.cards}))
    review = result["novelty_reviews"][0]
    assert review.incomplete_reason == "semantic_evidence"
    assert review.summary_harness_state["discarded_card_ids"] == ["C-1"]
    assert review.highly_relevant_works[0].card_ids == ["C-2"]
    saved = json.loads((tmp_path / "outputs/paper-1/reviewer/point-reviews.json").read_text())
    assert saved["reviews"][0]["summary_harness_state"]["adopted_card_ids"] == ["C-2"]
    cards = json.loads((tmp_path / "outputs/paper-1/reviewer/card-reviews.json").read_text())
    assert [r["review"] for r in cards["card_reviews"]] == [r["review"] for r in before]
    assert rows == before and len(client.calls) == 3
