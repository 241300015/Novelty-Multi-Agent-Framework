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


def test_handoff_wire_roundtrip_and_inheritance_binding_are_verified():
    request, rows, _ = sample()
    compact, _ = _compact_summary_rows(request, rows)
    handoffs = _summary_model_rows(compact)
    choices = SummaryChoices(handoffs)
    wire = choices.model_handoffs()
    assert choices.restore_handoffs(wire) == handoffs
    assert len(_verify_summary_payload(compact, json.dumps({"card_reviews": wire}), choices=choices)) == 6
    wire[0]["review"]["feature_comparisons"][0]["reason"] = "tampered"
    with pytest.raises(ValueError, match="handoff"):
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
    refs = schema["properties"]["material_evidence_choices"]["items"]["enum"]
    assert len(refs) == 2  # original card quote and newly registered Reader result
    assert result.review_evidence[0].evidence_id not in json.dumps(schema)
    wire = json.loads(client.valid.content)
    assert wire["material_evidence_choices"] == [refs[-1]]


@pytest.mark.parametrize("raw", [False, True])
def test_single_card_rejects_typed_option_mistakes_and_raw_ids(raw):
    class Invalid(ChoosingClient):
        async def acomplete(self, messages, *, options=None):
            response = await super().acomplete(messages, options=options)
            if self.calls[-1][2]["state"]["stage"] == "draft":
                value = json.loads(response.content)
                if raw:
                    value["material_evidence_choices"] = ["E-1"]
                else:
                    value["material_evidence_choices"] = ["w00000000_1"]
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
            props = schema["properties"]["feature_judgments"]["properties"]["comparison_1"]["anyOf"][0]["properties"]
            assert props["evidence_choices"]["maxItems"] == 0
            return ModelResponse(content=json.dumps({"material_assessment": "无可用原文",
                "material_evidence_choices": [], "supplement_request": None,
                "feature_judgments": {"comparison_1": {"judgment_choice": props["judgment_choice"]["enum"][0],
                    "basis_choice": props["basis_choice"]["enum"][0],
                    "attribution_choice": props["attribution_choice"]["enum"][-1],
                    "evidence_choices": [], "reason": "无法确认"}}}))
    result = asyncio.run(reviewer(Empty(read=False)).review_card(request))
    assert result.status.value == "reviewed" and result.feature_comparisons[0].evidence_refs == []
