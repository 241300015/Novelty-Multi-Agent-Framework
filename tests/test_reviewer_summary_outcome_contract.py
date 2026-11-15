"""The status selected by the model must fix the final wire contract."""
from copy import deepcopy

import pytest
from novelty_agent_framework.core.reviewer_choices import SummaryChoices
from novelty_agent_framework.core.reviewer_summary import SummaryVerification
from novelty_agent_framework.agents.evidence_reviewer import _compact_summary_rows, _summary_model_rows
from novelty_agent_framework.workflows.reviewer_summary_policy import ReviewerSummaryPolicy
from test_reviewer_summary_handoff import sample
from test_reviewer_strict_schema import assert_strict_objects


def policy():
    request, rows, _ = sample()
    compact, _ = _compact_summary_rows(request, rows)
    return ReviewerSummaryPolicy(request, SummaryChoices(_summary_model_rows(compact)),
        SummaryVerification(request, compact, max_chars=32000, max_requests=2))


def test_reviewed_end_choice_has_nonnullable_verdict_reason_and_confidence():
    p = policy()
    key = next(k for k, a in p.actions.items() if a[0] == "finish")
    p.resolve(p.selection({"option_id": key}, "end"))
    schema = p.draft_schema()
    assert_strict_objects(schema)
    assert schema["properties"]["status"] == {"type": "string", "enum": ["reviewed"]}
    for field in ["verdict", "verdict_reason", "confidence"]:
        assert "anyOf" not in schema["properties"][field]
    assert schema["properties"]["verdict_reason"]["minLength"] == 1
    assert "anyOf" not in schema  # Strict OpenAI root remains an object.


def test_insufficient_is_an_explicit_available_end_choice_and_cannot_be_upgraded():
    p = policy()
    before = deepcopy(p.cards)
    key = next(k for k, a in p.actions.items() if a[0] == "insufficient")
    p.resolve(p.selection({"option_id": key}, "end"))
    assert p.draft_schema()["properties"]["verdict"] == {"type": "null"}
    value = {"status": "insufficient_evidence", "verdict": None, "verdict_reason": "有关键缺口",
             "confidence": None, "supplement_request": None}
    assert p.assemble(value).verdict is None
    with pytest.raises(ValueError):
        p.assemble({**value, "status": "reviewed", "verdict": "novel", "confidence": .8})
    assert p.cards == before


def test_reviewed_cannot_be_silently_downgraded_or_accept_captured_null_verdict():
    p = policy()
    key = next(k for k, a in p.actions.items() if a[0] == "finish")
    p.resolve(p.selection({"option_id": key}, "end"))
    for status in ["reviewed", "insufficient_evidence"]:
        with pytest.raises(ValueError):
            p.assemble({"status": status, "verdict": None, "verdict_reason": "证据仍不足",
                        "confidence": .5, "supplement_request": None})


@pytest.mark.parametrize("field,value", [("verdict", None), ("verdict_reason", None),
    ("verdict_reason", ""), ("confidence", None), ("confidence", -1), ("confidence", 2)])
def test_reviewed_domain_and_wire_constraints_reject_incomplete_outcome(field, value):
    p = policy()
    key = next(k for k, a in p.actions.items() if a[0] == "finish")
    p.resolve(p.selection({"option_id": key}, "end"))
    valid = {"status": "reviewed", "verdict": "partially_novel", "verdict_reason": "有限比较",
             "confidence": 0, "supplement_request": None}
    assert p.assemble(valid).confidence == 0
    with pytest.raises(ValueError):
        p.assemble({**valid, field: value})


def test_actual_summary_repair_uses_same_selected_outcome_schema_and_keeps_card_facts():
    from test_reviewer_summary_policy import WireClient, run
    case = sample()
    original = deepcopy(case[1][0]["review"])
    def response(client, repair):
        return {"status": "reviewed", "verdict": "partially_novel" if repair else None,
                "verdict_reason": "有限比较", "confidence": .5, "supplement_request": None}
    result, client = run(WireClient(["作出点级裁定"], final=response), case=case)
    assert result.status.value == "reviewed"
    assert len(client.calls) == 3
    assert client.calls[-1][1].response_format == client.calls[-2][1].response_format
    schema = client.calls[-1][1].response_format["json_schema"]["schema"]
    assert schema["properties"]["verdict"] == {"$ref": "#/$defs/NoveltyVerdict"}
    assert result.model_dump(mode="json")["feature_comparisons"] == original["feature_comparisons"]
