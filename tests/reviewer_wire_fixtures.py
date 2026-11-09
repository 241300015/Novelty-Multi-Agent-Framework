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


def translate(value, original, projected, *, inherit=False):
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
    if inherit and isinstance(original, list) and "recheck" not in result:
        for domain, selection in (("feature_comparisons", "inherit_comparisons"),
                                   ("highly_relevant_works", "inherit_works")):
            remaining, selected = [], []
            for entry in result.get(domain, []):
                match = next((v for row in projected if row.get("review")
                    for v in row["review"][domain]
                    if {k: val for k, val in v.items() if k != "inherit_choice"} == entry), None)
                if match:
                    selected.append(match["inherit_choice"])
                else:
                    remaining.append(entry)
            result[domain] = remaining
            result[selection] = selected
        result["revised_comparisons"] = result.pop("feature_comparisons", [])
        result["additional_works"] = result.pop("highly_relevant_works", [])
    return result


def summary_response(client, response, messages):
    from novelty_agent_framework.agents.evidence_reviewer import _compact_summary_rows, _summary_model_rows
    from test_novelty_point_reviewer import _request, _indexed_review
    try:
        payload = json.loads(messages[1].content)
    except ValueError:
        return response
    if "card_reviews" in payload:
        request, rows = getattr(client, "summary_fixture", (_request(), [_indexed_review()]))
        client.fixture_wire = (_summary_model_rows(_compact_summary_rows(request, rows)[0]), payload["card_reviews"])
    if not hasattr(client, "fixture_wire") or response.content is None:
        return response
    try:
        value = json.loads(response.content)
    except ValueError:
        return response
    return replace(response, content=json.dumps(translate(value, *client.fixture_wire, inherit=True)))
