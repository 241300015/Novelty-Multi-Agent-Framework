"""Translate domain test fixtures into selections from the actual model request.

Production does not accept domain-ID output. These helpers let older semantic
fixtures exercise the new wire boundary without changing their expected facts.
"""
from copy import deepcopy
import json
from dataclasses import replace

FIELD_NAMES = {"evidence_id": "evidence_choice", "evidence_ids": "evidence_choices",
    "evidence_refs": "evidence_selections", "work_id": "work_choice",
    "card_id": "card_choice", "card_ids": "card_choices", "origin_card_id": "origin_card_choice"}


def translate(value, original, projected):
    mapping = {}

    def bind(a, b):
        if isinstance(a, list) and isinstance(b, list):
            for left, right in zip(a, b):
                bind(left, right)
        elif isinstance(a, dict) and isinstance(b, dict):
            for field, item in a.items():
                target = FIELD_NAMES.get(field, field)
                if target not in b:
                    continue
                if field in FIELD_NAMES:
                    left = item if isinstance(item, list) else [item]
                    right = b[target] if isinstance(b[target], list) else [b[target]]
                    mapping.update(zip(left, right))
                else:
                    bind(item, b[target])
    bind(original, projected)

    def encode(item):
        if isinstance(item, list):
            return [encode(v) for v in item]
        if not isinstance(item, dict):
            return item
        return {FIELD_NAMES.get(k, k): ([mapping.get(v, "invalid_option") for v in val]
                if isinstance(val, list) else mapping.get(val, "invalid_option"))
                if k in FIELD_NAMES else encode(val) for k, val in item.items()}
    result = encode(value)
    return result


def card_judgment_fixture(value, context):
    """Adapt old semantic fixtures; production never accepts these old fields."""
    menu = context["judgment_menu"]
    relation_labels = {"supported": "已读材料明确披露", "partially_supported": "已读材料仅披露",
                       "contradicted": "已读材料明确与", "unknown": "已读材料不足以判断"}
    basis_labels = {"direct_statement": "被引用原文直接", "semantic_equivalence": "原文采用不同",
                    "grounded_inference": "从所选原文", "insufficient": "原文不足以"}
    attribution_labels = {"author_method": "所选参考文献作者自身", "related_work": "所选参考文献介绍",
                          "baseline": "所选参考文献中的对比", "ablation": "所选参考文献中的消融",
                          "unknown": "已读材料不足以"}
    def choice(items, prefix):
        return next((key for key, label in items.items() if label.startswith(prefix)), "invalid_option")
    result = {"material_assessment": value.get("support_reason", "当前材料仅支持有限事实，保留未知。"),
              "material_evidence_choices": [e for w in value.get("highly_relevant_works", []) for e in w["evidence_choices"]],
              "feature_judgments": {}, "supplement_request": value.get("supplement_request")}
    for row in value.get("feature_comparisons", []):
        group = next((g for g in menu["groups"] if g["target_feature_id"] == row["feature_id"]
                      and g["reference_work_choice"] == row["work_choice"]), {"judgments": {}})
        item = {"judgment_choice": choice(group["judgments"], relation_labels[row["relation"]]),
            "basis_choice": choice(menu["basis_choices"], basis_labels[row["basis_type"]]),
            "attribution_choice": choice({k:v["meaning"] for k,v in menu["attribution_choices"].items()
                if v["reference_work_choice"] == row["work_choice"]}, attribution_labels[row.get("source_context", "unknown")]),
            "reason": row["reason"]}
        if "evidence_selections" in row:
            item["evidence_choices"] = row["evidence_selections"]
        result["feature_judgments"][group.get("slot", "invalid_slot")] = item
    return result


def summary_menu_response(messages, response=None):
    """Legacy semantic fixtures adopt all cards; dedicated wire tests choose explicitly."""
    from backend.env import ModelResponse
    if not any((m.content or "").startswith('{"summary_state":') for m in messages):
        return None
    menu = json.loads(messages[-1].content.split("\n")[1])
    if menu:
        try:
            status = json.loads(getattr(response, "content", response) or "{}").get("status")
        except (ValueError, TypeError):
            status = None
        label = "作出点级裁定" if status == "reviewed" else "证据不足"
        key = next((key for key, value in menu.items() if label in value), next(iter(menu)))
        return ModelResponse(content=json.dumps({"option_id": key}))
    return None


def summary_response(client, response, messages):
    # Compatibility is confined to test fixtures. Production accepts narrative only.
    if any((m.content or "").startswith('{"summary_state":') for m in messages):
        client.summary_narrative_fixture = True
    if not getattr(client, "summary_narrative_fixture", False) or response.content is None:
        return response
    try:
        value = json.loads(response.content)
    except ValueError:
        return response
    if isinstance(value, dict):
        value = {k: v for k, v in value.items() if k not in
                 {"novelty_point_id", "feature_comparisons", "highly_relevant_works", "report_disposition"}}
    return replace(response, content=json.dumps(value))
