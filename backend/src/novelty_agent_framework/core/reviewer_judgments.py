"""Harness-bound single-card judgments; selections do not prove entailment."""
from __future__ import annotations

from copy import deepcopy

from pydantic import Field

from .review_validation import feature_catalog
from .reviewer_choices import ReviewerSelectionError
from ..schemas.domain import ReviewerSupportDraft, StrictModel, SupplementRequest


RELATIONS = {
    "supported": "已读材料明确披露该技术特征的关键机制及必要条件；仅出现术语或方法名称不够。",
    "partially_supported": "已读材料仅披露该特征的一部分机制；指出已确认部分和未确认的必要条件。",
    "contradicted": "已读材料明确与该特征不符：同一对象、条件下有不相容的机制或约束；未提及、未详细描述或无法确认都不算不符。",
    "unknown": "已读材料不足以判断该特征：未提及、未读到或无法确认机制；不表示整篇文献没有该特征。",
}
BASES = {
    "direct_statement": "被引用原文直接陈述所选参考文献与技术特征的关系。",
    "semantic_equivalence": "原文采用不同术语但机制等价；解释对应关系。",
    "grounded_inference": "从所选原文明示的机制和条件作有限推断；说明对应与限制，不能从缺少描述推断不符。",
    "insufficient": "原文不足以确定关系；只用于不足以判断的选项。",
}
ATTRIBUTIONS = {
    "author_method": "所选参考文献作者自身提出或采用的方法；不是目标论文作者。",
    "related_work": "所选参考文献介绍的他人工作或背景方法。",
    "baseline": "所选参考文献中的对比基线。",
    "ablation": "所选参考文献中的消融设置。",
    "unknown": "已读材料不足以确认方法归属。",
}


class JudgmentSelection(StrictModel):
    judgment_choice: str = Field(min_length=1)
    basis_choice: str = Field(min_length=1)
    attribution_choice: str = Field(min_length=1)
    evidence_choices: list[str]
    reason: str = Field(min_length=1, description="解释所选原文如何支持该参考文献与该技术特征的关系；不能用目标论文主张代替证据。")


class CardJudgmentDraft(StrictModel):
    material_assessment: str = Field(min_length=1, description="说明这张参考文献卡的已读材料能确认什么、还缺什么；不要复述目标论文贡献或裁定新颖性。")
    material_evidence_choices: list[str]
    feature_judgments: dict[str, JudgmentSelection]
    supplement_request: SupplementRequest | None


class ReviewerJudgments:
    def __init__(self, request, references, ledger):
        self.request, self.references, self.ledger = request, references, ledger
        for kind in ("b", "a"):
            references.bindings[kind] = {}
        self.features = feature_catalog(request.novelty_point)
        self.works = list(dict.fromkeys(e.work_id for e in request.evidence
            if e.evidence_id in request.cards[0].evidence_ids))
        self.required = {(work, f["feature_id"]) for work in self.works for f in self.features}
        self.slots = {f"comparison_{i+1}": pair for i, pair in enumerate(
            (work, f["feature_id"]) for work in self.works for f in self.features)}
        for work in self.works:
            for feature in self.features:
                for relation in RELATIONS:
                    references.register("j", {"work_id": work, "feature_id": feature["feature_id"], "relation": relation})
            for context in ATTRIBUTIONS:
                references.register("a", {"work_id": work, "source_context": context})
        for basis in BASES:
            references.register("b", basis)

    def menu(self):
        refs = self.references
        groups = []
        for work in self.works:
            for feature in self.features:
                groups.append({"slot": next(slot for slot, pair in self.slots.items() if pair==(work,feature["feature_id"])),
                    "reference_work_choice": refs.choice("w", work),
                    "reference_card_title": self.request.cards[0].document_title,
                    "target_feature_id": feature["feature_id"], "target_feature": feature["text"],
                    "judgments": {key: RELATIONS[item["relation"]] for key, item in refs.bindings["j"].items()
                        if (item["work_id"], item["feature_id"]) == (work, feature["feature_id"])
                        and (item["relation"] == "unknown" or self._evidence_choices(work))}})
        return {"instruction": "目标论文的 claim 是待比较对象，不是参考文献事实。feature_judgments 是对象，按每组 slot 填写；每个槽位只能选本组判断，不能跨槽位复用判断。关系描述参考文献披露了什么，不是证明目标论文正确。",
            "groups": groups,
            "basis_choices": {key: BASES[value] for key, value in refs.bindings["b"].items()},
            "attribution_choices": {key: {"reference_work_choice": refs.choice("w", item["work_id"]),
                "meaning": ATTRIBUTIONS[item["source_context"]]} for key, item in refs.bindings["a"].items()}}

    def _evidence_choices(self, work=None):
        return [self.references.choice("e", eid) for eid, info in self.ledger.choices().items()
                if work is None or info["work_id"] == work]

    @staticmethod
    def _enum(values):
        return {"type": "string", "enum": values or ["no_available_option"]}

    def schema(self):
        schema = CardJudgmentDraft.model_json_schema()
        selections = schema["$defs"].pop("JudgmentSelection")
        slots = {}
        for slot, (work, fid) in self.slots.items():
            branches = []
            for unknown in (True, False):
                branch = deepcopy(selections)
                p = branch["properties"]
                p["judgment_choice"] = self._enum([key for key, value in self.references.bindings["j"].items()
                    if (value["work_id"],value["feature_id"]) == (work,fid)
                    and (value["relation"] == "unknown") == unknown])
                p["basis_choice"] = self._enum([key for key, value in self.references.bindings["b"].items()
                    if (value == "insufficient") == unknown])
                p["attribution_choice"] = self._enum([key for key, value in self.references.bindings["a"].items()
                    if value["work_id"] == work])
                evidence = self._evidence_choices(work)
                p["evidence_choices"]["items"] = self._enum(evidence)
                if not unknown:
                    p["evidence_choices"]["minItems"] = 1
                if not evidence:
                    p["evidence_choices"]["maxItems"] = 0
                if unknown or evidence:  # No non-unknown option can be grounded without evidence.
                    branches.append(branch)
            slots[slot] = {"anyOf": branches}
        evidence = self._evidence_choices()
        schema["properties"]["material_evidence_choices"]["items"] = self._enum(evidence)
        if not evidence:
            schema["properties"]["material_evidence_choices"]["maxItems"] = 0
        schema["properties"]["feature_judgments"] = {"type":"object", "properties":slots,
            "required":list(slots), "additionalProperties":False}
        return schema

    def compile(self, value):
        def reject_raw_bindings(node):
            if isinstance(node, list):
                for child in node:
                    reject_raw_bindings(child)
            elif isinstance(node, dict):
                if set(node).intersection({"novelty_point_id", "feature_id", "work_id", "work_choice",
                        "relation", "basis_type", "source_context", "card_ids", "card_choices",
                        "evidence_id", "evidence_ids", "evidence_refs", "highly_relevant_works"}):
                    raise ReviewerSelectionError("select Harness-bound judgments; do not rewrite identities or relation fields")
                for child in node.values():
                    reject_raw_bindings(child)
        reject_raw_bindings(value)
        wire = CardJudgmentDraft.model_validate(value)
        refs, comparisons, seen = self.references, [], set()
        work_refs = {work: [] for work in self.works}
        work_reasons = {work: [] for work in self.works}

        def resolve_evidence(choices, work=None):
            evidence = [refs.resolve("e", key) for key in choices]
            for eid in evidence:
                item = self.ledger.choices().get(eid)
                if item is None or (work is not None and item["work_id"] != work):
                    raise ReviewerSelectionError("judgment evidence must belong to its bound reference Work")
            return list(dict.fromkeys(evidence))

        for eid in resolve_evidence(wire.material_evidence_choices):
            work = self.ledger.choices()[eid]["work_id"]
            work_refs[work].append(eid)
            if wire.material_assessment not in work_reasons[work]:
                work_reasons[work].append(wire.material_assessment)
        if set(wire.feature_judgments) != set(self.slots):
            raise ReviewerSelectionError("missing or foreign judgment slot; fill every Harness-provided slot")
        for slot, selection in wire.feature_judgments.items():
            binding = refs.resolve("j", selection.judgment_choice)
            key = (binding["work_id"], binding["feature_id"])
            if key != self.slots[slot] or key in seen:
                raise ReviewerSelectionError("select exactly one judgment for each reference Work and target feature")
            seen.add(key)
            attribution = refs.resolve("a", selection.attribution_choice)
            if attribution["work_id"] != binding["work_id"]:
                raise ReviewerSelectionError("attribution must belong to the judgment's reference Work")
            basis = refs.resolve("b", selection.basis_choice)
            unknown = binding["relation"] == "unknown"
            if unknown != (basis == "insufficient"):
                raise ReviewerSelectionError("unknown requires insufficient basis; a confirmed relation requires a grounded basis")
            evidence = resolve_evidence(selection.evidence_choices, binding["work_id"])
            if not unknown and not evidence:
                raise ReviewerSelectionError("non-unknown judgment requires same-Work evidence")
            comparisons.append({**binding, "basis_type": basis, "source_context": attribution["source_context"],
                "evidence_refs": evidence, "reason": selection.reason})
            work_refs[binding["work_id"]].extend(evidence)
            if evidence:
                work_reasons[binding["work_id"]].append(f'{binding["feature_id"]}：{selection.reason}')
        if seen != self.required:
            raise ReviewerSelectionError("missing reference Work / feature judgment; select unknown when evidence is insufficient")
        # This is a record of the model's selected references and explanations,
        # not a new relevance or novelty adjudication by the Harness.
        works = [{"work_id": work, "card_ids": [self.request.cards[0].card_id],
            "evidence_ids": list(dict.fromkeys(evidence)), "relevance_reason": "\n".join(work_reasons[work])}
            for work, evidence in work_refs.items() if evidence]
        return ReviewerSupportDraft(novelty_point_id=self.request.novelty_point.point_id,
            support_reason=wire.material_assessment, highly_relevant_works=works,
            feature_comparisons=comparisons, supplement_request=wire.supplement_request)
