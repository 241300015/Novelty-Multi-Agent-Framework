"""Single-card stages. All mutable state belongs to one invocation."""
from __future__ import annotations

import json
import uuid
from copy import deepcopy
from dataclasses import replace

from ..core.reviewer_choices import ReviewerChoices

from backend.env import ModelToolCall

from ..core.reviewer_evidence import ReviewerEvidenceLedger, reviewer_model_view
from ..schemas.domain import CardSupportStatus


class ReviewerReadingPolicy:
    pending_instruction = "先完成当前材料支撑性评估和阅读决策；读后必须重新评估，菜单为空才提交说明。"

    def __init__(self, registry, request, *, catalog, max_read_chars, max_tool_calls):
        self.registry = registry
        self.catalog = deepcopy(catalog)
        self.ledger = ReviewerEvidenceLedger(request, self.catalog)
        self.references = ReviewerChoices()
        self.references.register("c", request.cards[0].card_id)
        for item in request.evidence:
            if item.evidence_id in request.cards[0].evidence_ids:
                self.references.register("w", item.work_id)
        self._sync_references()
        self.remaining_chars, self.remaining_tools = max_read_chars, max_tool_calls
        self.prefix, self.version = uuid.uuid4().hex, 0
        self.stage = "assess_card"
        self.support_history, self.read_history, self.transitions = [], [], [self.stage]
        self.observations = []
        self.reading_decision = "not_selected"
        self.offsets = {}
        self.finished = set()
        self.pending = None
        self._support_menu()

    @property
    def can_finish(self):
        return self.stage == "draft"

    def _stage(self, stage):
        self.stage = stage
        self.transitions.append(stage)

    def _set(self, actions):
        self.version += 1
        self.actions = {f"rv_{self.prefix}_{self.version}_{i}": action for i, action in enumerate(actions)}

    def _support_menu(self):
        labels = ["可用", "部分可用", "待核验", "不可用"]
        self._set([("support", status.value, label) for status, label in zip(CardSupportStatus, labels)])

    def _read_menu(self):
        actions = [("stop", None, "结束阅读并重新评估" if self.read_history else "不读取，保留当前材料局限")]
        if "reader" in self.registry.names and self.remaining_tools > 0 and self.remaining_chars > 0:
            # Offer saved body text first; an abstract remains an explicit option.
            # Role and extent are separate: a complete abstract is not a full paper.
            for entry in sorted(self.catalog, key=lambda e: e["role"] != "extracted_text"):
                address = (entry["namespace"], entry["artifact_id"])
                # The existing Reader interface uses bare IDs; ambiguous addresses are never authorized.
                if sum(e["artifact_id"] == entry["artifact_id"] for e in self.catalog) != 1:
                    continue
                if address not in self.finished:
                    actions.append(("read", address, f"读取 {entry['artifact_id']} ({entry['namespace']}, "
                                    f"类型={entry['role']}, 范围={entry['content_extent']}, "
                                    f"可读={entry['readable']})"))
        self._set(actions)

    def choices(self):
        return {key: value[2] for key, value in self.actions.items()}

    def selection(self, arguments, call_id):
        return ModelToolCall(id=call_id, name="choose_reviewer_action", arguments=arguments)

    def resolve(self, call):
        if (call.name != "choose_reviewer_action" or set(call.arguments) != {"option_id"}
                or not isinstance(call.arguments["option_id"], str)
                or call.arguments["option_id"] not in self.actions):
            raise ValueError("select a current invocation's option_id")
        kind, value, _ = self.actions[call.arguments["option_id"]]
        if kind == "support":
            self.support_history.append({"stage": self.stage, "status": value})
            if self.stage == "assess_card":
                self._stage("decide_reading")
                self._read_menu()
            else:
                self._stage("draft")
                self._set([])
            return None
        if kind == "stop":
            if self.read_history:
                self._stage("reassess_card")
                self._support_menu()
            else:
                self.reading_decision = "skip"
                self._stage("draft")
                self._set([])
            return None
        entry = next(e for e in self.catalog if (e["namespace"], e["artifact_id"]) == value)
        start = self.offsets.get(value, 0)
        available = entry.get("readable_chars")
        chars = min(self.registry.get("reader").default_chars_per_read, self.remaining_chars,
                    max(1, available - start) if isinstance(available, int) else self.remaining_chars)
        self.remaining_tools -= 1
        self.remaining_chars -= chars  # Reserve requested characters, including failed reads; no refund.
        self.reading_decision = "read"
        self._stage("reading")
        self.pending = value
        self._set([])
        return ModelToolCall(id=call.id, name="reader", arguments={
            "artifact_id": entry["artifact_id"], "char_start": start, "max_chars": chars})

    def observe(self, call, observation):
        self.observations.append(observation)
        record = {"namespace": self.pending[0], **call.arguments,
                  "succeeded": observation.succeeded, "registered": False, "artifact_read_complete": False}
        self.read_history.append(record)
        self.last_evidence = None
        raw = observation.payload.get("read_result")
        if observation.succeeded:
            if not isinstance(raw, dict):
                raise ValueError("successful Reader observation has no read result")
            self.last_evidence = self.ledger.register(raw, call)
            self._sync_references()
            record.update(registered=True, read_id=raw["read_id"], char_end=raw["char_end"])
            self.offsets[self.pending] = raw["char_end"]
            entry = next(e for e in self.catalog if (e["namespace"], e["artifact_id"]) == self.pending)
            record["artifact_read_complete"] = (raw["char_end"] == entry.get("readable_chars")
                                                and not raw.get("has_more", True))
        if not observation.succeeded or not raw.get("has_more", True) or raw["char_end"] <= raw["char_start"]:
            self.finished.add(self.pending)
        if self.remaining_tools > 0 and self.remaining_chars > 0:
            self._stage("decide_reading")
            self._read_menu()
            if len(self.actions) > 1:
                return
        self._stage("reassess_card")
        self._support_menu()

    def project_model_context(self, observation, context):
        item = self.last_evidence
        return self.references.project({"succeeded": observation.succeeded, "error": observation.error,
                "evidence": ([] if item is None else [{"evidence_id": item.evidence_id,
                    "work_id": item.work_id, "quote": item.exact_quote,
                    "role": item.role, "content_extent": item.content_extent}])})

    def snapshot(self):
        return deepcopy({"stage": self.stage, "transitions": self.transitions,
            "support_history": self.support_history, "reading_decision": self.reading_decision,
            "read_history": self.read_history, "remaining_read_chars": self.remaining_chars,
            "remaining_tool_calls": self.remaining_tools, "material_catalog": self.catalog})

    def _sync_references(self):
        for eid, info in self.ledger.choices().items():
            self.references.register("e", eid)
            self.references.register("w", info["work_id"])

    def draft_schema(self):
        from ..agents.reviewer_card import _strict_support_schema
        from ..schemas.domain import ReviewerSupportDraft
        return _strict_support_schema(self.references.schema(ReviewerSupportDraft.model_json_schema()))

    def final_options(self, options):
        return replace(options, tools=(), tool_choice="none", response_format={
            "type": "json_schema", "json_schema": {"name": "reviewer_support_draft",
                "strict": True, "schema": self.draft_schema()}})

    def model_context(self):
        result = {"state": self.references.project(reviewer_model_view(self.snapshot())),
                  "evidence_choices": {self.references.choice("e", eid): self.references.project(info)
                                       for eid, info in self.ledger.choices().items()}}
        if self.can_finish:
            result["review_schema"] = self.draft_schema()
        return json.dumps(result, ensure_ascii=False)

    def complete(self):
        if not self.can_finish:
            raise ValueError("cannot complete pending Reviewer stages")
        self._stage("complete")
