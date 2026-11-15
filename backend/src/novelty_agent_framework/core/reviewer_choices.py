"""Invocation-owned model selections; persistent identities never come from model text.

Only structural identity fields are translated. Scientific prose is never rewritten.
The domain models and the retained evidence ledger keep their original identities.
"""
from __future__ import annotations

from copy import deepcopy
import secrets


# Domain field -> (wire field, identity namespace, collection).
FIELDS = {
    "evidence_id": ("evidence_choice", "e", False),
    "evidence_ids": ("evidence_choices", "e", True),
    "evidence_refs": ("evidence_selections", "e", True),
    "work_id": ("work_choice", "w", False),
    "card_id": ("card_choice", "c", False),
    "card_ids": ("card_choices", "c", True),
    "origin_card_id": ("origin_card_choice", "c", False),
}
REVERSE = {v[0]: (k, v[1], v[2]) for k, v in FIELDS.items()}


class ReviewerSelectionError(ValueError):
    """Selection authorization failure, never eligible for fuzzy repair."""


class ReviewerChoices:
    def __init__(self):
        self.scope = secrets.token_hex(4)
        self.bindings = {kind: {} for kind in ("e", "w", "c", "j", "r")}

    def register(self, kind, value):
        for choice, saved in self.bindings[kind].items():
            if saved == value:
                return choice
        choice = f"{kind}{self.scope}_{len(self.bindings[kind]) + 1}"
        self.bindings[kind][choice] = deepcopy(value)
        return choice

    def resolve(self, kind, choice):
        if not isinstance(choice, str) or choice not in self.bindings[kind]:
            raise ReviewerSelectionError(f"unknown or stale {kind} selection; select a current Harness option")
        return deepcopy(self.bindings[kind][choice])

    def choice(self, kind, value):
        for choice, saved in self.bindings[kind].items():
            if saved == value:
                return choice
        raise ReviewerSelectionError(f"identity absent from Harness {kind} choices")

    def project(self, value):
        """Project trusted input; callers register only authorized evidence first."""
        if isinstance(value, list):
            return [self.project(v) for v in value]
        if not isinstance(value, dict):
            return deepcopy(value)
        result = {}
        for key, item in value.items():
            if key in FIELDS:
                wire, kind, many = FIELDS[key]
                result[wire] = [self.choice(kind, v) for v in item] if many else self.choice(kind, item)
            else:
                result[key] = self.project(item)
        return result

    def compile(self, value):
        """Reject raw IDs, foreign namespaces and stale options, including in local output."""
        if isinstance(value, list):
            return [self.compile(v) for v in value]
        if not isinstance(value, dict):
            return deepcopy(value)
        result = {}
        for key, item in value.items():
            if key in FIELDS:
                raise ReviewerSelectionError(f"model must select Harness options, not emit {key}")
            if key in REVERSE:
                domain, kind, many = REVERSE[key]
                if many and not isinstance(item, list):
                    raise ReviewerSelectionError(f"{key} must be an option list")
                result[domain] = [self.resolve(kind, v) for v in item] if many else self.resolve(kind, item)
            else:
                result[key] = self.compile(item)
        return result

    def schema(self, schema):
        """Copy and bind schema fields to the exact current option enums."""
        result = deepcopy(schema)

        def visit(node):
            if not isinstance(node, dict):
                return
            properties = node.get("properties", {})
            for key in list(properties):
                if key in FIELDS:
                    wire, kind, many = FIELDS[key]
                    field = properties.pop(key)
                    properties[wire] = field
                    if "required" in node:
                        node["required"] = [wire if name == key else name for name in node["required"]]
                    choices = list(self.bindings[kind])
                    if choices:
                        (field["items"] if many else field)["enum"] = choices
                    elif many:
                        field["maxItems"] = 0
                    else:
                        # No legitimate scalar selection; runtime rejects this sentinel too.
                        field["enum"] = ["no_available_option"]
            for keyword in ("properties", "$defs", "definitions"):
                for child in node.get(keyword, {}).values():
                    visit(child)
            visit(node.get("items"))
            for keyword in ("anyOf", "oneOf", "allOf", "prefixItems"):
                for child in node.get(keyword, []):
                    visit(child)
        visit(result)
        return result


class SummaryChoices(ReviewerChoices):
    def __init__(self, handoffs):
        super().__init__()
        for handoff in handoffs:
            self.register("c", handoff["card_id"])
            for evidence in handoff["selected_evidence"]:
                self.register("e", evidence["evidence_id"])
                self.register("w", evidence["work_id"])
            review = handoff.get("review")
            if review:
                for work in review["highly_relevant_works"]:
                    self.register("w", work["work_id"])
                for comparison in review["feature_comparisons"]:
                    self.register("w", comparison["work_id"])
        self.handoffs = deepcopy(handoffs)

    def model_handoffs(self):
        result = self.project(self.handoffs)
        for row in result:
            row["contract_version"] = "card_choices_v3"
        return result

    def restore_handoffs(self, rows):
        result = deepcopy(rows)
        for row in result:
            if row.get("contract_version") != "card_choices_v3":
                raise ValueError("unexpected model handoff version")
            row["contract_version"] = "card_judgment_v1"
        return self.compile(result)
