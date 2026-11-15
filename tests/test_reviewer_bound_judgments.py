"""Bound reference/feature choices at both the contract and real orchestration seam."""
import asyncio
from copy import deepcopy
import json

import pytest
from backend.env import ModelResponse
from novelty_agent_framework.core.reviewer_choices import ReviewerSelectionError
from novelty_agent_framework.schemas.domain import ReviewerSupportDraft
from novelty_agent_framework.tools import ResearcherToolRegistry
from novelty_agent_framework.workflows.reviewer_reading_policy import ReviewerReadingPolicy
from test_novelty_point_reviewer import _request
from test_reviewer_controlled_reading import ChoosingClient, reviewer
from test_reviewer_strict_schema import assert_strict_objects


def policy(*, two_works=False, empty=False):
    request = _request()
    request.novelty_point.technical_features = ["固定规模映射", "重构误差学习"]
    if empty:
        request.evidence[0].quote = ""
    if two_works:
        request.evidence.append(request.evidence[0].model_copy(update={
            "evidence_id": "E-2", "work_id": "work-2", "artifact_id": "artifact-2"}))
        request.cards[0].evidence_ids.append("E-2")
    p = ReviewerReadingPolicy(ResearcherToolRegistry([]), request, catalog=[], max_read_chars=10, max_tool_calls=1)
    return p, request


def wire(p, relation="unknown"):
    result = {"material_assessment": "参考材料能确认的机制及局限。", "material_evidence_choices": [],
              "feature_judgments": {}, "supplement_request": None}
    r = p.references
    for slot, (work, fid) in p.judgments.slots.items():
        result["feature_judgments"][slot] = {
            "judgment_choice": r.choice("j", {"work_id": work, "feature_id": fid, "relation": relation}),
            "basis_choice": r.choice("b", "insufficient" if relation=="unknown" else "direct_statement"),
            "attribution_choice": r.choice("a", {"work_id": work, "source_context": "related_work"}),
            "evidence_choices": [] if relation=="unknown" else [r.choice("e", next(eid for eid, info in p.ledger.choices().items() if info["work_id"]==work))],
            "reason": f"仅说明参考文献 {work} 对 {fid} 的依据。"}
    return result


@pytest.mark.parametrize("relation", ["supported", "partially_supported", "contradicted", "unknown"])
def test_reference_and_feature_are_harness_bound_and_source_input_is_unchanged(relation):
    p, request = policy(two_works=True)
    before = request.model_dump(mode="json")
    value = wire(p, relation)
    result = p.judgments.compile(value)
    assert {(f.work_id,f.feature_id) for f in result.feature_comparisons} == p.judgments.required
    assert all(f.relation==relation and f.source_context=="related_work" for f in result.feature_comparisons)
    assert result.novelty_point_id == request.novelty_point.point_id
    assert request.model_dump(mode="json") == before
    assert all(not {"work_id","feature_id","relation","basis_type","source_context"}.intersection(v)
               for v in value["feature_judgments"].values())
    for work in result.highly_relevant_works:
        assert work.card_ids == [request.cards[0].card_id]
        assert all(next(e.work_id for e in request.evidence if e.evidence_id==eid)==work.work_id for eid in work.evidence_ids)


@pytest.mark.parametrize("problem", ["missing", "duplicate", "foreign_judgment", "foreign_evidence",
    "foreign_attribution", "wrong_kind", "no_evidence", "insufficient_basis", "raw_identity"])
def test_invalid_bound_selections_fail_closed(problem):
    p, _ = policy(two_works=True)
    value = wire(p, "supported")
    row = value["feature_judgments"]["comparison_1"]
    if problem=="missing":
        value["feature_judgments"].pop("comparison_4")
    elif problem=="duplicate":
        value["feature_judgments"]["comparison_4"] = deepcopy(row)
    elif problem=="foreign_judgment":
        other, _ = policy(two_works=True)
        row["judgment_choice"] = wire(other,"supported")["feature_judgments"]["comparison_1"]["judgment_choice"]
    elif problem=="foreign_evidence":
        row["evidence_choices"] = [p.references.choice("e","E-2")]
    elif problem=="foreign_attribution":
        row["attribution_choice"] = p.references.choice("a",{"work_id":"work-2","source_context":"author_method"})
    elif problem=="wrong_kind":
        row["judgment_choice"] = row["evidence_choices"][0]
    elif problem=="no_evidence":
        row["evidence_choices"] = []
    elif problem=="insufficient_basis":
        row["basis_choice"] = p.references.choice("b","insufficient")
    else:
        row["feature_id"] = "F2"
    with pytest.raises(ReviewerSelectionError):
        p.judgments.compile(value)


def test_no_original_text_offers_only_unknown_and_empty_lists_are_valid():
    p, _ = policy(empty=True)
    menu = p.judgments.menu()
    assert all(len(g["judgments"])==1 for g in menu["groups"])
    result = p.judgments.compile(wire(p))
    assert all(f.relation=="unknown" and not f.evidence_refs for f in result.feature_comparisons)
    assert not result.highly_relevant_works
    schema = p.draft_schema()
    assert_strict_objects(schema)
    assert schema["properties"]["material_evidence_choices"]["maxItems"]==0
    assert len(schema["properties"]["feature_judgments"]["properties"]["comparison_1"]["anyOf"])==1


def test_schema_is_strict_work_scoped_and_does_not_mutate_domain_schema():
    p, _ = policy(two_works=True)
    before = deepcopy(ReviewerSupportDraft.model_json_schema())
    schema = p.draft_schema()
    assert_strict_objects(schema)
    slots = schema["properties"]["feature_judgments"]["properties"]
    assert len(slots)==4
    for slot, definition in slots.items():
        for branch in definition["anyOf"]:
            props = branch["properties"]
            bindings = [p.references.resolve("j",j) for j in props["judgment_choice"]["enum"]]
            assert {(b["work_id"], b["feature_id"]) for b in bindings} == {p.judgments.slots[slot]}
            works = {b["work_id"] for b in bindings}
            assert {p.ledger.choices()[p.references.resolve("e",e)]["work_id"] for e in props["evidence_choices"]["items"]["enum"]}==works
    assert ReviewerSupportDraft.model_json_schema()==before
    assert not {"novelty_point_id","support_reason","highly_relevant_works"}.intersection(schema["properties"])


@pytest.mark.parametrize("read", [False, True])
def test_live_orchestration_uses_new_menu_and_repairs_only_same_wire_schema(read):
    request = _request()
    request.novelty_point.technical_features = ["固定规模映射", "重构误差学习"]
    class Repair(ChoosingClient):
        async def acomplete(self, messages, *, options=None):
            if messages[0].content.startswith("Repair the format"):
                assert json.loads(messages[1].content)["schema"]==self.calls[-1][1].response_format["json_schema"]["schema"]
                return self.saved
            response = await super().acomplete(messages,options=options)
            if self.calls[-1][2]["state"]["stage"]=="draft":
                self.saved = response
                return ModelResponse(content="{invalid")
            return response
    client = Repair(read=read)
    result = asyncio.run(reviewer(client).review_card(request))
    assert result.status.value=="reviewed"
    assert len(result.feature_comparisons)==2
    assert bool(result.review_evidence)==read
    final = client.calls[-1][2]
    assert len(final["judgment_menu"]["groups"])==2
    assert "目标论文" in final["judgment_menu"]["instruction"]
    assert all("参考文献" in v["meaning"] or "归属" in v["meaning"] for v in final["judgment_menu"]["attribution_choices"].values())
    assert "material_assessment" in json.loads(client.saved.content)


def test_no_features_keeps_old_domain_compatibility_without_inventing_a_feature():
    request = _request()
    p = ReviewerReadingPolicy(ResearcherToolRegistry([]), request, catalog=[], max_read_chars=10, max_tool_calls=1)
    result = p.judgments.compile({"material_assessment":"只有材料说明", "material_evidence_choices":[p.references.choice("e","E-1")],
        "feature_judgments":{}, "supplement_request":None})
    assert result.feature_comparisons==[]
    assert result.highly_relevant_works[0].evidence_ids==["E-1"]
    assert ReviewerSupportDraft.model_validate(result.model_dump())==result
