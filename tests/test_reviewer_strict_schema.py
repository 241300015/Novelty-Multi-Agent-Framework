"""Offline checks at the actual Reviewer request and transport boundaries."""
import asyncio
from copy import deepcopy
import json

import pytest
from backend.env import ModelProfile, ModelResponse, OpenAICompatibleChatClient
from novelty_agent_framework.schemas import ReviewerSupportDraft
from test_reviewer_controlled_reading import ChoosingClient, reviewer
from test_novelty_point_reviewer import _request


def assert_strict_objects(node, root=None):
    root = node if root is None else root
    if isinstance(node, dict):
        if node.get("type") == "object" or "properties" in node:
            assert set(node.get("required", [])) == set(node["properties"])
            assert node["additionalProperties"] is False
        if "$ref" in node:
            assert node["$ref"].startswith("#/$defs/")
            assert node["$ref"].split("/")[-1] in root["$defs"]
        for value in node.values():
            assert_strict_objects(value, root)
    elif isinstance(node, list):
        for value in node:
            assert_strict_objects(value, root)


@pytest.mark.parametrize("read", [False, True])
@pytest.mark.parametrize("endpoint", ["http://localhost:8000/v1", "https://api.openai.com/v1"])
def test_actual_requests_are_strict_and_preserve_menu_switch(read, endpoint):
    original = deepcopy(ReviewerSupportDraft.model_json_schema())
    client = ChoosingClient(read=read)
    result = asyncio.run(reviewer(client).review_card(_request()))
    assert result.status.value == "reviewed"
    transport = OpenAICompatibleChatClient(ModelProfile(alias="offline", model="offline", base_url=endpoint))
    for messages, options, context in client.calls:
        fmt = transport._build_payload(messages, options)["response_format"]["json_schema"]
        assert fmt["strict"] is True
        assert_strict_objects(fmt["schema"])
        if context["state"]["stage"] != "draft":
            assert fmt["name"] == "harness_decision"
            assert set(fmt["schema"]["properties"]) == {"option_id"}
        else:
            assert fmt["name"] == "reviewer_support_draft"
            assert json.loads(messages[1].content)["review_schema"] == fmt["schema"]
    assert ReviewerSupportDraft.model_json_schema() == original


def test_format_repair_uses_same_strict_contract():
    class RepairClient(ChoosingClient):
        async def acomplete(self, messages, *, options=None):
            if messages[0].content.startswith("Repair the format"):
                self.repair = (messages, options)
                return ModelResponse(content=json.dumps({"novelty_point_id": "NP-1",
                    "support_reason": "证据有限", "highly_relevant_works": [],
                    "feature_comparisons": [], "supplement_request": None}))
            response = await super().acomplete(messages, options=options)
            return ModelResponse(content="{invalid") if self.calls[-1][2]["state"]["stage"] == "draft" else response
    client = RepairClient(read=False)
    result = asyncio.run(reviewer(client).review_card(_request()))
    assert result.status.value == "reviewed"
    messages, options = client.repair
    assert options.response_format == client.calls[-1][1].response_format
    schema = options.response_format["json_schema"]["schema"]
    assert_strict_objects(schema)
    assert json.loads(messages[1].content)["schema"] == schema
    assert not options.tools and options.tool_choice == "none"


@pytest.mark.parametrize("relation,refs,accepted", [
    ("unknown", [], True), ("supported", ["E-1"], True),
    ("partially_supported", ["E-1"], True), ("contradicted", ["E-1"], True),
    ("supported", [], False), ("supported", None, False), ("supported", ["missing"], False),
])
def test_evidence_contract_survives_strict_conversion(relation, refs, accepted):
    request = _request()
    request.novelty_point.technical_features = ["待比较的技术特征"]
    eid = request.cards[0].evidence_ids[0]
    refs = [eid if value == "E-1" else value for value in refs] if refs is not None else None
    client = ChoosingClient(read=False, draft={"supplement_request": None,
        "feature_comparisons": [{"feature_id": "F1", "work_id": "work-1",
            "relation": relation, "basis_type": "insufficient" if relation == "unknown" else "direct_statement",
            "evidence_refs": refs, "reason": "只说明片段所支持的范围", "source_context": "unknown"}]})
    if refs is None:
        del client.draft["feature_comparisons"][0]["evidence_refs"]
    result = asyncio.run(reviewer(client).review_card(request))
    assert (result.status.value == "reviewed") is accepted
    schema = client.calls[-1][1].response_format["json_schema"]["schema"]
    unknown, grounded = schema["$defs"]["FeatureComparison"]["anyOf"]
    assert unknown["properties"]["relation"]["const"] == "unknown"
    assert "minItems" not in unknown["properties"]["evidence_refs"]
    assert grounded["properties"]["evidence_refs"]["minItems"] == 1
    assert grounded["properties"]["relation"]["enum"] == ["supported", "partially_supported", "contradicted"]
    assert schema["properties"]["supplement_request"]["anyOf"] == [
        {"$ref": "#/$defs/SupplementRequest"}, {"type": "null"}]
    assert schema["properties"]["feature_comparisons"]["type"] == "array"


def test_normalization_does_not_mutate_input_or_domain_defaults():
    from novelty_agent_framework.agents.reviewer_card import _strict_support_schema
    schema = ReviewerSupportDraft.model_json_schema()
    before = deepcopy(schema)
    wire = _strict_support_schema(schema)
    assert schema == before
    assert wire is not schema
    assert_strict_objects(wire)
    assert "default" not in wire["properties"]["supplement_request"]
    draft = ReviewerSupportDraft(novelty_point_id="NP-1", support_reason="历史数据")
    assert draft.supplement_request is None and draft.feature_comparisons == []
    assert "highly_relevant_works" not in schema["required"]


@pytest.mark.parametrize("supplement", [None, {"reason": "需要更完整材料", "missing_aspects": [], "suggested_focus": []}])
def test_explicit_empty_lists_and_nullable_supplement(supplement):
    client = ChoosingClient(read=False, draft={"highly_relevant_works": [],
        "feature_comparisons": [], "supplement_request": supplement})
    result = asyncio.run(reviewer(client).review_card(_request()))
    assert result.status.value == "reviewed"
    assert result.highly_relevant_works == [] and result.feature_comparisons == []
    assert (result.supplement_request is None) is (supplement is None)
    schema = client.calls[-1][1].response_format["json_schema"]["schema"]
    assert_strict_objects(schema)
    nested = schema["$defs"]["SupplementRequest"]
    assert nested["properties"]["missing_aspects"]["type"] == "array"
    assert nested["properties"]["reason"]["minLength"] == 1
