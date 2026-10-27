"""Card support selection precedes the independent decision to read full text.

One policy belongs to one Card invocation. The model chooses issued options;
only the runtime owns states, Artifact IDs, offsets and read budgets.
"""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass

from backend.env import ModelToolCall, ToolDefinition

from ..schemas.domain import CardSupportStatus
from ..core.reviewer_evidence import ReviewerEvidenceRegistry, reviewer_model_view
from ..schemas.failures import FailureCode as C, CodedFailure
from ..tools.reviewer_database_search import ReviewerAcquisitionScope, ReviewerDatabaseSearchTool


SUPPORT_LABELS = {
    CardSupportStatus.USABLE: "可用：仅凭当前 Card 和手头资料，认为可用于后续比较",
    CardSupportStatus.PARTIAL: "部分可用：当前资料只足以使用 Card 的部分内容",
    CardSupportStatus.NEEDS_VERIFICATION: "待核验：当前资料不足以判断 Card 是否可用",
    CardSupportStatus.UNUSABLE: "不可用：当前资料显示 Card 暂不能用于比较，需说明具体问题",
}

READ_EFFECT_LABELS = {
    "helpful": "回读有帮助：补充了判断依据或澄清、纠正了初判",
    "no_new_information": "回读没有新增有用信息：仍保留原有依据和判断边界",
    "unresolved": "回读效果尚无法判断：关键问题仍未解决或读取未完成",
}


@dataclass(frozen=True)
class _Action:
    label: str
    kind: str
    arguments: dict


class ReviewerReadingPolicy:
    pending_instruction = "先选择当前菜单中的编号；支撑性登记后才决定是否读全文。菜单为空后提交单卡说明。"

    def __init__(self, registry, scope, *, catalog, max_read_chars, max_tool_calls):
        if len(scope.cards) != 1:
            raise ValueError("Reviewer reading policy requires exactly one Card")
        self.registry, self.scope = registry, scope
        self.catalog = catalog
        self.remaining_chars = max_read_chars
        self.remaining_tools = max_tool_calls
        self.prefix = uuid.uuid4().hex[:12]
        self.version = 0
        self.issued = set()
        self.actions = {}
        self.support_history = []
        self.read_history = []
        self.observations = []
        self.evidence_registry = ReviewerEvidenceRegistry(scope, catalog)
        self.evidence = self.evidence_registry.evidence
        self.read_origin = "local"
        self.acquisition_history = []
        self.acquisition_attempts = set()
        self.acquisition_blocked = None
        self.reading_decision = "not_selected"
        self.active = None
        self.offset = 0
        self.reading_complete = False
        self.reading_effect = None
        self.stage = "assess_card"
        self._support_menu()

    @property
    def can_finish(self):
        return self.stage == "draft"

    @property
    def can_transition_without_tools(self):
        return all(action.kind not in {"read", "acquire"} for action in self.actions.values())

    def _set(self, actions):
        self.version += 1
        self.actions = {f"rv_{self.prefix}_{self.version}_{i}": action
                        for i, action in enumerate(actions)}

    def _support_menu(self):
        self._set([_Action(label, "support", {"status": status.value})
                   for status, label in SUPPORT_LABELS.items()])

    def _reading_menu(self):
        self.stage = "decide_reading"
        actions = [_Action("不继续读全文，保留当前支撑性与局限并提交单卡说明", "skip", {})]
        if "reader" in self.registry.names and self.remaining_chars > 0 and self.remaining_tools > 0:
            # The full catalog remains in the prompt even when no read is legal.
            local_bodies = [entry for entry in self.catalog if entry.get("role") == "extracted_text"]
            # With no saved body, attempt the bound local abstract/material first.
            # Even a missing file goes through Reader and yields an audited failure.
            for entry in local_bodies or self.catalog:
                label = "继续阅读正文" if entry.get("role") == "extracted_text" else "尝试回读本地材料"
                actions.append(_Action(
                    f"{label} {entry['artifact_id']}（{entry.get('readable_chars')} 字符，"
                    f"材料范围 {entry.get('content_extent', 'unknown')}，可读={entry.get('readable')}）",
                    "read", {"artifact_id": entry["artifact_id"], "char_start": 0}))
        self._set(actions)

    def _local_attempts(self):
        return tuple(dict.fromkeys(r["artifact_id"] for r in self.read_history if r["origin"] == "local"))

    def _after_local_reading(self):
        self.stage = "reassess_card"
        self._support_menu()
        if (not self._local_attempts() or "database_search" not in self.registry.names
                or self.remaining_tools < 2 or self.remaining_chars <= 0):
            return
        database = self.registry.get("database_search")
        if not isinstance(database, ReviewerDatabaseSearchTool):
            return
        try:
            candidates = database.acquisition_catalog(self.scope,
                local_attempt_artifact_ids=self._local_attempts())
        except (OSError, ValueError) as exc:
            self.acquisition_blocked = "source_manifest_unavailable:" + type(exc).__name__
            return
        actions = [_Action(f"在线获取当前论文全文：{item['title']}（{item['source_id']}）", "acquire", item)
                   for item in candidates if item["source_record_id"] not in self.acquisition_attempts]
        if actions:
            self.stage = "decide_acquisition"
            self._set([_Action("不在线获取，保留当前材料并重新判断可用性", "skip_acquisition", {}), *actions])

    def execution_scope(self, call):
        if call.name == "database_search":
            return ReviewerAcquisitionScope(request=self.scope, source_id=call.arguments["source_id"],
                source_record_id=call.arguments["full_text_source_record_ids"][0],
                local_attempt_artifact_ids=self._local_attempts())
        return self.scope

    def choices(self):
        self.issued.update(self.actions)
        return {key: action.label for key, action in self.actions.items()}

    def definitions(self):
        return (ToolDefinition(name="choose_reviewer_action", description="选择当前 Reviewer 状态或动作",
            parameters={"type": "object", "properties": {"option_id": {
                "type": "string", "enum": list(self.actions)}},
                "required": ["option_id"], "additionalProperties": False}),) if self.actions else ()

    def selection(self, arguments, call_id):
        return ModelToolCall(id=call_id, name="choose_reviewer_action", arguments=arguments)

    def resolve(self, call):
        if call.name != "choose_reviewer_action" or set(call.arguments) != {"option_id"}:
            raise CodedFailure(C.HARNESS_DECISION_SCHEMA, "return only a current option_id")
        option = call.arguments["option_id"]
        if not isinstance(option, str) or option not in self.actions:
            code = (C.HARNESS_DECISION_SCHEMA if not isinstance(option, str) else
                    C.HARNESS_OPTION_STALE if option in self.issued else
                    C.HARNESS_OPTION_LABEL if option in self.choices().values() else C.HARNESS_OPTION_UNKNOWN)
            raise CodedFailure(code, "unissued or stale Reviewer option")
        action = self.actions[option]
        if action.kind == "support":
            self.support_history.append({"stage": self.stage, "status": action.arguments["status"],
                                         "option_id": option})
            if self.stage == "assess_card":
                self._reading_menu()
            else:
                self.stage = "assess_read_effect"
                self._set([_Action(label, "read_effect", {"effect": effect})
                           for effect, label in READ_EFFECT_LABELS.items()])
            return None
        if action.kind == "read_effect":
            self.reading_effect = action.arguments["effect"]
            self.stage = "draft"
            self._set([])
            return None
        if action.kind == "skip":
            self.reading_decision = "skip"
            self.stage = "draft"
            self._set([])
            return None
        if action.kind == "stop_reading":
            if self.read_origin == "local":
                self._after_local_reading()
            else:
                self.stage = "reassess_card"
                self._support_menu()
            return None
        if action.kind == "skip_acquisition":
            self.stage = "reassess_card"
            self._support_menu()
            return None
        if action.kind == "acquire":
            if not self._local_attempts() or self.remaining_tools < 2:
                raise CodedFailure(C.TOOL_SCOPE, "acquisition requires a local read attempt and budget to read the result")
            self.acquisition_attempts.add(action.arguments["source_record_id"])
            self.remaining_tools -= 1
            self.stage = "acquiring"
            return ModelToolCall(id=call.id, name="database_search", arguments={
                "source_id": action.arguments["source_id"],
                "full_text_source_record_ids": [action.arguments["source_record_id"]]})
        self.reading_decision = "read_fulltext"
        self.stage = "reading"
        self.active = action.arguments["artifact_id"]
        self.offset = action.arguments["char_start"]
        entry = next(e for e in self.catalog if e["artifact_id"] == self.active)
        reader = self.registry.get("reader")
        available = entry.get("readable_chars")
        chars = min(reader.default_chars_per_read, self.remaining_chars,
                    max(1, available - self.offset) if isinstance(available, int) else reader.default_chars_per_read)
        if chars <= 0 or self.remaining_tools <= 0:
            raise CodedFailure(C.BUDGET_READ_CHARS, "no remaining Reviewer reading budget")
        self.remaining_chars -= chars
        self.remaining_tools -= 1
        return ModelToolCall(id=call.id, name="reader", arguments={
            "artifact_id": self.active, "char_start": self.offset, "max_chars": chars})

    def observe(self, call, observation):
        self.observations.append(observation)
        if call.name == "database_search":
            self._after_acquisition(call, observation)
            return
        read = observation.payload.get("read_result", {})
        self.read_history.append({"artifact_id": self.active, "char_start": self.offset,
            "succeeded": observation.succeeded, "read_id": read.get("read_id"),
            "char_end": read.get("char_end"), "origin": self.read_origin})
        if observation.succeeded and read.get("char_end", 0) > self.offset:
            self.offset = read["char_end"]
            length = next(e["readable_chars"] for e in self.catalog if e["artifact_id"] == self.active)
            self.reading_complete = not read.get("has_more", True) and self.offset == length
            if not self.reading_complete and self.remaining_chars > 0 and self.remaining_tools > 0:
                self._set([
                    _Action("继续读取当前正文下一段", "read", {
                        "artifact_id": self.active, "char_start": self.offset}),
                    _Action("停止读取，保留未读范围并重新判断支撑性", "stop_reading", {}),
                ])
                return
        if self.read_origin == "local":
            self._after_local_reading()
        else:
            self.stage = "reassess_card"
            self._support_menu()

    def _after_acquisition(self, call, observation):
        record_id = call.arguments["full_text_source_record_ids"][0]
        # Rebuild permissions from persisted files, not the provider/model's text.
        refreshed = self.registry.get("reader").material_catalog(self.scope)
        self.catalog[:] = refreshed
        entries = [entry for entry in refreshed if entry.get("source_record_id") == record_id
                   and entry.get("role") == "extracted_text" and entry.get("readable")
                   and (entry.get("readable_chars") or 0) > 0]
        self.acquisition_history.append({"source_id": call.arguments["source_id"],
            "source_record_id": record_id, "tool_succeeded": observation.succeeded,
            "outcome": "readable_fulltext" if entries else "no_readable_authorized_fulltext",
            "artifact_ids": [entry["artifact_id"] for entry in entries]})
        self.stage = "reassess_card"
        self._support_menu()
        if entries and self.remaining_tools > 0 and self.remaining_chars > 0:
            self.read_origin = "acquired"
            self.reading_complete = False
            self.stage = "read_acquired"
            # Acquisition cannot directly finalize. A real Reader observation
            # must precede re-assessment of the newly downloaded material.
            self._set([_Action(f"读取新获取的正文 {entry['artifact_id']}", "read", {
                "artifact_id": entry["artifact_id"], "char_start": 0}) for entry in entries])

    def snapshot(self):
        return {"stage": self.stage, "support_history": list(self.support_history),
                "reading_decision": self.reading_decision,
                "reading_effect": self.reading_effect,
                "artifact_id": self.active, "read_history": list(self.read_history),
                "artifact_read_complete": self.reading_complete,
                "remaining_read_chars": self.remaining_chars,
                "remaining_tool_calls": self.remaining_tools,
                "acquisition_history": list(self.acquisition_history),
                "acquisition_blocked": self.acquisition_blocked,
                "material_catalog": self.catalog}

    def model_context(self):
        return "Reviewer Harness state:\n" + json.dumps(reviewer_model_view(self.snapshot()), ensure_ascii=False)

    def project_model_context(self, observation, context):
        return self.evidence_registry.project_model_context(observation, context)
