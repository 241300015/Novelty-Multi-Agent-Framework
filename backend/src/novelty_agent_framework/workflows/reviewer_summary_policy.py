"""Point-level choices: adopt card judgments, inspect a target, or discard a card.

The model never reconstructs comparisons. The invocation owns menus, frozen
targets and the assembled result; retained evidence stays outside default input.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import json
import uuid

from backend.env import ModelToolCall

from ..core.reviewer_choices import ReviewerSelectionError
from ..schemas import ResearcherToolObservation, ReviewStatus
from ..schemas.domain import ReviewerSummaryDraft
from ..schemas.reviewer_handoff import ReviewerSummaryNarrative, SummaryRecheckTarget


class SummaryRecheckTool:
    name = "reviewer_recheck"
    description = "回看指定单卡已选依据；不检索、不重新评审单卡。"
    args_schema = SummaryRecheckTarget

    def __init__(self, verification, *, on_record=None):
        self.verification = verification
        self.on_record = on_record

    async def ainvoke(self, arguments, *, scope):
        record = self.verification.recheck(arguments)
        if self.on_record is not None:
            self.on_record(record)
        return ResearcherToolObservation(tool_name=self.name,
            arguments=arguments.model_dump(mode="json"), succeeded=True,
            payload={"targeted_verification": record})


class ReviewerSummaryPolicy:
    pending_instruction = "点级阶段尚未结束。仅选择当前 option_id；不要填写或修改单卡判断。"

    def __init__(self, request, choices, verification):
        self.request, self.references, self.verification = request, choices, verification
        self.cards = {h["card_id"]: deepcopy(h) for h in choices.handoffs
                      if h["status"] == "completed" and h.get("review")}
        self.active = list(self.cards)
        self.discarded, self.decisions, self.observations = [], [], []
        self.prefix, self.version = uuid.uuid4().hex, 0
        self.stage, self.transitions = "select_judgments", ["select_judgments"]
        self.pending = None
        self.force_insufficient = False
        self._menu()

    @property
    def can_finish(self):
        return self.stage == "draft"

    def _stage(self, stage):
        self.stage = stage
        self.transitions.append(stage)

    def _set(self, actions):
        self.version += 1
        self.actions = {f"rs_{self.prefix}_{self.version}_{i}": action
                        for i, action in enumerate(actions)}

    def _conflicts(self):
        groups = {}
        for cid in self.active:
            for c in self.cards[cid]["review"]["feature_comparisons"]:
                key = (c["work_id"], c["feature_id"])
                groups.setdefault(key, set()).add((c["relation"], c["basis_type"], c["source_context"]))
        return [{"work_id": w, "feature_id": f} for (w, f), values in groups.items() if len(values) > 1]

    def _menu(self):
        conflict = bool(self._conflicts())
        # The model selects the outcome before drafting. This binds the flat
        # strict schema without unsupported root unions or automatic verdicts.
        actions = [
            ("insufficient", None, "采用剩余单卡判断，按证据不足收尾（insufficient_evidence；verdict 为 null）"),
            ("finish", None, "采用剩余单卡判断，作出点级裁定（reviewed；须有裁定、理由和置信度）"),
        ]
        if not self.active or conflict:
            actions = [("insufficient", None, "按证据不足收尾，保留现有判断及冲突局限")]
        for cid in self.active:
            label = self.references.choice("c", cid)
            actions.append(("discard", cid, f"放弃这张单卡判断：{label}"))
            if len(self.observations) >= self.verification.max_requests:
                continue
            for comparison in self.cards[cid]["review"]["feature_comparisons"]:
                if not comparison["evidence_refs"]:
                    continue
                target = SummaryRecheckTarget(card_id=cid, work_id=comparison["work_id"],
                    feature_id=comparison["feature_id"], evidence_ids=comparison["evidence_refs"],
                    question="核对已选依据是否支持这张单卡的现有特征判断；仅选择保留或放弃整张卡，不改写判断。")
                # Do not offer a partial quote as if it were the full evidence.
                chars = sum(len(getattr(self.verification.sources[eid], "exact_quote",
                                       getattr(self.verification.sources[eid], "quote", "")))
                            for eid in target.evidence_ids)
                if 0 < chars <= self.verification.max_chars - self.verification.chars:
                    actions.append(("recheck", target, f"定向复核 {label} / {comparison['feature_id']} 的已选依据"))
        self._set(actions)

    def choices(self):
        return {key: action[2] for key, action in self.actions.items()}

    def selection(self, arguments, call_id):
        return ModelToolCall(id=call_id, name="choose_summary_action", arguments=arguments)

    def resolve(self, call):
        if (call.name != "choose_summary_action" or set(call.arguments) != {"option_id"}
                or not isinstance(call.arguments["option_id"], str)
                or call.arguments["option_id"] not in self.actions):
            raise ReviewerSelectionError("select a current summary option_id")
        kind, value, _ = self.actions[call.arguments["option_id"]]
        self.decisions.append({"stage": self.stage, "option_id": call.arguments["option_id"],
            "action": kind, "target": value.model_dump(mode="json") if kind == "recheck" else value})
        if kind in {"finish", "insufficient"}:
            self.force_insufficient = kind == "insufficient"
            self._stage("draft")
            self._set([])
        elif kind in {"discard", "keep"}:
            if kind == "discard":
                self.active.remove(value)
                self.discarded.append(value)
            self.pending = None
            self._stage("select_judgments")
            self._menu()
        else:
            self.pending = value
            self._stage("rechecking")
            self._set([])
            return ModelToolCall(id=call.id, name=SummaryRecheckTool.name,
                                 arguments=value.model_dump(mode="json"))
        return None

    def observe(self, call, observation):
        self.observations.append(observation)
        self._stage("decide_rechecked_card")
        cid = self.pending.card_id
        self._set([("keep", cid, "保留这张单卡的原判断，继续选择"),
                   ("discard", cid, "放弃这张单卡判断，继续选择")])

    def project_model_context(self, observation, context):
        if observation.succeeded:
            return self.references.project(observation.payload)
        return {"succeeded": False, "error": "本次复核未完成，不能视为确认单卡判断。"}

    def snapshot(self):
        return deepcopy({"stage": self.stage, "transitions": self.transitions,
            "adopted_card_ids": self.active if self.stage in {"draft", "complete"} else [],
            "remaining_card_ids": self.active, "discarded_card_ids": self.discarded,
            "decisions": self.decisions, "force_insufficient": self.force_insufficient,
            "rechecks": [{"target": obs.arguments, "succeeded": obs.succeeded}
                         for obs in self.observations]})

    def draft_schema(self):
        from ..agents.reviewer_card import _strict_support_schema
        schema = _strict_support_schema(ReviewerSummaryNarrative.model_json_schema())
        status = "insufficient_evidence" if self.force_insufficient else "reviewed"
        schema["properties"]["status"] = {"type": "string", "enum": [status]}
        if self.force_insufficient:
            schema["properties"]["verdict"] = {"type": "null"}
        else:
            schema["properties"]["verdict"] = {"$ref": "#/$defs/NoveltyVerdict"}
            schema["properties"]["verdict_reason"] = {"type": "string", "minLength": 1}
            schema["properties"]["confidence"] = {"type": "number", "minimum": 0, "maximum": 1}
        return schema

    def final_options(self, options):
        return replace(options, tools=(), tool_choice="none", response_format={
            "type": "json_schema", "json_schema": {"name": "reviewer_summary_narrative",
                "strict": True, "schema": self.draft_schema()}})

    def model_context(self):
        # Do not project snapshot's internal control fields as model-authored IDs.
        result = {"summary_state": {"stage": self.stage,
            "remaining_card_choices": [self.references.choice("c", cid) for cid in self.active],
            "discarded_card_choices": [self.references.choice("c", cid) for cid in self.discarded],
            "conflicts": self.references.project(self._conflicts()),
            "remaining_rechecks": self.verification.max_requests - len(self.observations),
            "remaining_recheck_chars": self.verification.max_chars - self.verification.chars,
            "instruction": "只综合剩余卡片判断；被放弃卡片不能作为本次点级结论的依据。"}}
        if self.can_finish:
            result["review_schema"] = self.draft_schema()
        return json.dumps(result, ensure_ascii=False)

    def assemble(self, payload):
        if not self.can_finish:
            raise ValueError("cannot assemble before summary choices finish")
        narrative = ReviewerSummaryNarrative.model_validate(payload)
        selected_status = ReviewStatus.INSUFFICIENT_EVIDENCE if self.force_insufficient else ReviewStatus.REVIEWED
        if narrative.status is not selected_status:
            raise ValueError(f"return the model-selected summary status: {selected_status.value}; do not change the selected outcome")
        facts = {}
        for field in ("feature_comparisons", "highly_relevant_works"):
            entries = [entry for cid in self.active for entry in self.cards[cid]["review"][field]]
            # Only byte-equivalent structured facts collapse. Never merge or
            # pick between different judgments for the same Work/feature.
            facts[field] = list({json.dumps(entry, sort_keys=True, ensure_ascii=False): deepcopy(entry)
                                 for entry in entries}.values())
        return ReviewerSummaryDraft(novelty_point_id=self.request.novelty_point.point_id,
                                    **narrative.model_dump(), **facts)

    def comparison_origins(self, review):
        return [{"feature_id": f.feature_id, "work_id": f.work_id, "mode": "inherited",
                 "assembly": "harness_card_selection", "evidence_refs": f.evidence_refs,
                 "card_ids": [cid for cid in self.active
                              if f.model_dump(mode="json") in self.cards[cid]["review"]["feature_comparisons"]]}
                for f in review.feature_comparisons]

    def complete(self):
        if not self.can_finish:
            raise ValueError("cannot complete pending summary choices")
        self._stage("complete")
