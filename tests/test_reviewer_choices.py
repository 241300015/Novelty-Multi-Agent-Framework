"""Exercise actual wire selections, without translating domain-output fixtures."""
import asyncio
from copy import deepcopy
from dataclasses import replace
import json

import pytest
from backend.env import ModelResponse
from novelty_agent_framework.agents.evidence_reviewer import (
    NoveltyEvidenceReviewer, _compact_summary_rows, _summary_model_rows, _verify_summary_payload,
)
from novelty_agent_framework.core.reviewer_choices import SummaryChoices, ReviewerSelectionError
from test_reviewer_summary_handoff import sample
from test_reviewer_controlled_reading import ChoosingClient, reviewer
from test_novelty_point_reviewer import _request
from test_reviewer_strict_schema import assert_strict_objects


def inherited(payload):
    judgments = [row["review"] for row in payload["card_reviews"] if row["review"]]
    return {"novelty_point_id": payload["novelty_point"]["point_id"], "status": "insufficient_evidence",
        "verdict": None, "verdict_reason": None, "confidence": None,
        "inherit_comparisons": [c["inherit_choice"] for r in judgments for c in r["feature_comparisons"]],
        "inherit_works": [w["inherit_choice"] for r in judgments for w in r["highly_relevant_works"]],
        "revised_comparisons": [], "additional_works": [],
        "supplement_request": {"reason": "检索覆盖仍有限", "missing_aspects": [], "suggested_focus": []}}


class WireClient:
    def __init__(self, respond=None):
        self.respond, self.calls = respond, []

    async def acomplete(self, messages, *, options=None):
        self.calls.append((list(messages), options))
        if "Repair the format" not in messages[0].content:
            self.payload = json.loads(messages[1].content)
        output = self.respond(self, messages) if self.respond else inherited(self.payload)
        return ModelResponse(content=output if isinstance(output, str) else json.dumps(output))


def test_point_model_selects_inheritance_harness_copies_reason_and_real_ids_exactly():
    request, rows, _ = sample()
    before = deepcopy(rows)
    client = WireClient()
    result = asyncio.run(NoveltyEvidenceReviewer(client).summarize_reviews(request, rows))
    assert result.incomplete_reason == "semantic_evidence"
    assert [c.model_dump(mode="json") for c in result.feature_comparisons] == rows[0]["review"]["feature_comparisons"]
    assert [w.model_dump(mode="json") for w in result.highly_relevant_works] == rows[0]["review"]["highly_relevant_works"]
    assert rows == before and len(client.calls) == 1
    source = client.calls[0][0][1].content
    for forbidden in ('"evidence_id"', '"evidence_refs"', '"work_id"', '"card_id"', 'rev_ev_', 'SOURCE_'):
        assert forbidden not in source
    assert not inherited(client.payload)["revised_comparisons"]


@pytest.mark.parametrize("corruption", ["raw_id", "typo", "namespace", "duplicate"])
def test_summary_rejects_invalid_selections_without_fuzzy_repair(corruption):
    request, rows, _ = sample()
    def respond(client, messages):
        payload = client.payload
        draft = inherited(payload)
        if corruption == "duplicate":
            draft["inherit_comparisons"].append(draft["inherit_comparisons"][0])
        else:
            work = dict(payload["card_reviews"][0]["review"]["highly_relevant_works"][0])
            work.pop("inherit_choice")
            draft["inherit_works"] = []
            draft["additional_works"] = [work]
            if corruption == "raw_id":
                work["evidence_choices"] = ["rev_ev_f3449d9d9a94e1634b0ebfce9"]
            elif corruption == "typo":
                work["evidence_choices"][0] += "x"
            else:
                work["evidence_choices"] = [work["work_choice"]]
        return draft
    client = WireClient(respond)
    result = asyncio.run(NoveltyEvidenceReviewer(client).summarize_reviews(request, rows))
    assert result.incomplete_reason == "technical_error" and len(client.calls) == 1
    assert result.execution_issues[-1].code == "model.output_reference"


def test_previous_summary_choices_are_invalid_in_next_invocation():
    request, rows, _ = sample()
    saved = []
    def respond(client, messages):
        if not saved:
            saved.append(inherited(client.payload))
        return saved[0]
    client = WireClient(respond)
    service = NoveltyEvidenceReviewer(client)
    first = asyncio.run(service.summarize_reviews(request, rows))
    second = asyncio.run(service.summarize_reviews(request, rows))
    assert first.incomplete_reason == "semantic_evidence"
    assert second.incomplete_reason == "technical_error"
    assert len(client.calls) == 2


def test_targeted_recheck_compiles_selected_ids_and_returns_only_wire_choices():
    request, rows, _ = sample()
    def respond(client, messages):
        row = client.payload["card_reviews"][0]
        comparison = deepcopy(row["review"]["feature_comparisons"][2])
        if len(client.calls) == 1:
            return {"recheck": {"card_choice": row["card_choice"], "work_choice": comparison["work_choice"],
                "feature_id": "F3", "evidence_choices": comparison["evidence_selections"], "question": "复核第三项机制"}}
        verified = json.loads(messages[-1].content)["targeted_verification"]
        assert "evidence_id" not in json.dumps(verified) and "rev_ev_" not in json.dumps(verified)
        assert len(verified["quotes"]) == 2
        draft = inherited(client.payload)
        draft["inherit_comparisons"].remove(comparison.pop("inherit_choice"))
        comparison.update(relation="unknown", basis_type="insufficient", reason="已核验内容不能确认第三项机制")
        draft["revised_comparisons"] = [comparison]
        return draft
    client = WireClient(respond)
    result = asyncio.run(NoveltyEvidenceReviewer(client).summarize_reviews(request, rows))
    assert result.incomplete_reason == "semantic_evidence"
    assert result.feature_comparisons[-1].evidence_refs == ["rev_ev_4", "rev_ev_5"]
    assert result.feature_comparisons[-1].relation == "unknown"
    assert len(client.calls) == 2


@pytest.mark.parametrize("malformed", ["json", "copied_inheritance_field", "duplicated_inheritance"])
def test_summary_format_repair_reuses_inheritance_choice_schema(malformed):
    request, rows, _ = sample()
    def respond(client, messages):
        draft = inherited(client.payload)
        if len(client.calls) == 1:
            if malformed == "json":
                return "{invalid"
            draft["revised_comparisons"] = deepcopy(client.payload["card_reviews"][0]["review"]["feature_comparisons"])
            if malformed == "duplicated_inheritance":
                for item in draft["revised_comparisons"]:
                    item.pop("inherit_choice")
        return draft
    client = WireClient(respond)
    result = asyncio.run(NoveltyEvidenceReviewer(client).summarize_reviews(request, rows))
    assert result.incomplete_reason == "semantic_evidence" and len(client.calls) == 2
    repair = json.loads(client.calls[1][0][1].content)
    assert repair["schema"] == client.payload["review_schema"]
    assert result.feature_comparisons[2].evidence_refs == ["rev_ev_4", "rev_ev_5"]


def test_handoff_wire_roundtrip_and_inheritance_binding_are_verified():
    request, rows, _ = sample()
    compact, _ = _compact_summary_rows(request, rows)
    handoffs = _summary_model_rows(compact)
    choices = SummaryChoices(handoffs)
    wire = choices.model_handoffs()
    assert choices.restore_handoffs(wire) == handoffs
    assert len(_verify_summary_payload(compact, json.dumps({"card_reviews": wire}), choices=choices)) == 6
    wire[0]["review"]["feature_comparisons"][0]["reason"] = "tampered"
    with pytest.raises(ValueError, match="differs"):
        _verify_summary_payload(compact, json.dumps({"card_reviews": wire}), choices=choices)
    with pytest.raises(ReviewerSelectionError):
        choices.compile({"evidence_refs": ["rev_ev_0"]})


def test_single_card_new_reads_extend_final_enums_and_repair_keeps_binding():
    class Repair(ChoosingClient):
        async def acomplete(self, messages, *, options=None):
            if messages[0].content.startswith("Repair the format"):
                self.repair_options = options
                self.repair_schema = json.loads(messages[1].content)["schema"]
                return self.valid
            result = await super().acomplete(messages, options=options)
            if self.calls[-1][2]["state"]["stage"] == "draft":
                self.valid = result
                return ModelResponse(content="{invalid")
            return result
    client = Repair()
    result = asyncio.run(reviewer(client).review_card(_request()))
    assert result.status.value == "reviewed"
    assert result.highly_relevant_works[0].evidence_ids == [result.review_evidence[0].evidence_id]
    schema = client.calls[-1][1].response_format["json_schema"]["schema"]
    assert_strict_objects(schema)
    assert schema == client.repair_schema
    assert client.repair_options.response_format == client.calls[-1][1].response_format
    refs = schema["$defs"]["RelevantWork"]["properties"]["evidence_choices"]["items"]["enum"]
    assert len(refs) == 2  # original card quote and newly registered Reader result
    assert result.review_evidence[0].evidence_id not in json.dumps(schema)
    wire = json.loads(client.valid.content)
    assert wire["highly_relevant_works"][0]["evidence_choices"] == [refs[-1]]


@pytest.mark.parametrize("raw", [False, True])
def test_single_card_rejects_typed_option_mistakes_and_raw_ids(raw):
    class Invalid(ChoosingClient):
        async def acomplete(self, messages, *, options=None):
            response = await super().acomplete(messages, options=options)
            if self.calls[-1][2]["state"]["stage"] == "draft":
                value = json.loads(response.content)
                work = value["highly_relevant_works"][0]
                if raw:
                    work["evidence_ids"] = work.pop("evidence_choices")
                else:
                    work["evidence_choices"] = [work["work_choice"]]
                return replace(response, content=json.dumps(value))
            return response
    client = Invalid(read=False)
    result = asyncio.run(reviewer(client).review_card(_request()))
    assert result.incomplete_reason == "technical_error" and len(client.calls) == 3
    assert result.execution_issues[-1].code == "model.output_reference"


def test_empty_source_has_no_citable_options_but_can_preserve_unknown():
    request = _request()
    request.evidence[0].quote = ""
    request.novelty_point.technical_features = ["unknown mechanism"]
    class Empty(ChoosingClient):
        async def acomplete(self, messages, *, options=None):
            context = next(json.loads(m.content) for m in reversed(messages) if m.content.startswith('{"state":'))
            if context["state"]["stage"] != "draft":
                return await super().acomplete(messages, options=options)
            assert context["evidence_choices"] == {}
            schema = options.response_format["json_schema"]["schema"]
            assert_strict_objects(schema)
            props = schema["$defs"]["FeatureComparison"]["anyOf"][0]["properties"]
            assert props["evidence_selections"]["maxItems"] == 0
            return ModelResponse(content=json.dumps({"novelty_point_id": "NP-1", "support_reason": "无可用原文",
                "highly_relevant_works": [], "supplement_request": None,
                "feature_comparisons": [{"feature_id": "F1", "work_choice": props["work_choice"]["enum"][0],
                    "relation": "unknown", "basis_type": "insufficient", "evidence_selections": [],
                    "reason": "无法确认", "source_context": "unknown"}]}))
    result = asyncio.run(reviewer(Empty(read=False)).review_card(request))
    assert result.status.value == "reviewed" and result.feature_comparisons[0].evidence_refs == []
