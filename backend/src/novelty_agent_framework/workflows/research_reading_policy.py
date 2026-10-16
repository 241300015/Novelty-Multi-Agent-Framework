"""Research reading menus built from tool observations, never model-written IDs.

One instance belongs to one Harness invocation. The generic Harness only knows
how to present/resolve a policy; research phases and source handles live here.
"""
from __future__ import annotations

import json
from dataclasses import dataclass

from backend.env import ModelToolCall, ToolDefinition


@dataclass(frozen=True)
class _Action:
    label: str
    name: str
    arguments: dict


class ResearchReadingPolicy:
    def __init__(self, registry, scope, *, max_chars=8000):
        self.registry = registry
        self.scope = scope
        self.max_chars = max_chars
        self.sources = (tuple(registry.get("database_search").tools_by_source)
                        if "database_search" in registry.names else ())
        self.candidates = {}
        self.consumed = set()
        self.attempted_reads = set()
        self.exhausted_sources = set()
        self.active = None
        self.version = 0
        self.actions = {}
        directive = getattr(scope, "recovery", None)
        action = directive.action.value if directive else None
        if action in {"read_artifact", "fetch_fulltext"}:
            self.sources = ()
        if action == "read_artifact":
            self._set([self._read(a.artifact_id, 0, "读取补检指定材料")
                       for a in directive.artifacts])
        elif action == "fetch_fulltext":
            self._set([_Action("获取补检指定文献全文", "database_search", {
                "source_id": directive.source_id, "full_text_source_record_ids": [record]})
                for record in directive.source_record_ids])
        else:
            if directive and directive.source_id:
                self.sources = tuple(s for s in self.sources if s == directive.source_id)
            self._set(self._searches())

    def _searches(self):
        return [_Action(f"重新检索/选择 {source} 的其他未读候选（同一计划可能复用检索结果）",
                        "database_search", {"source_id": source})
                for source in self.sources if source not in self.exhausted_sources]

    def _read(self, artifact_id, start, label):
        return _Action(label, "reader", {"artifact_id": artifact_id,
                       "char_start": start, "max_chars": self.max_chars})

    def _set(self, actions):
        self.version += 1
        self.actions = {f"v{self.version}_{i}": action for i, action in enumerate(actions)
                        if action.name in self.registry.names}

    def choices(self):
        return {key: action.label for key, action in self.actions.items()}

    def selection(self, arguments, call_id):
        return ModelToolCall(id=call_id, name="choose_research_action", arguments=arguments)

    def definitions(self):
        if not self.actions:
            return ()
        return (ToolDefinition(name="choose_research_action", description=(
            "选择当前允许的下一步；Harness 执行工具并填写可信索引。也可直接提交最终 JSON。选项：" +
            json.dumps(self.choices(), ensure_ascii=False)),
            parameters={"type": "object", "properties": {
                "option_id": {"type": "string", "enum": list(self.actions)}},
                "required": ["option_id"], "additionalProperties": False}),)

    def resolve(self, call):
        if call.name != "choose_research_action" or set(call.arguments) != {"option_id"}:
            raise ValueError("select only option_id from the current action menu")
        option = call.arguments["option_id"]
        if not isinstance(option, str) or option not in self.actions:
            raise ValueError("unknown or stale option_id; select from the current menu")
        action = self.actions[option]
        return ModelToolCall(id=call.id, name=action.name, arguments=dict(action.arguments))

    def observe(self, call, observation):
        if call.name == "database_search":
            self._after_database(call, observation)
        elif call.name == "reader":
            self._after_read(call, observation)

    def _after_database(self, call, observation):
        acquisition = bool(call.arguments.get("full_text_source_record_ids"))
        if not observation.succeeded:
            # Failures are returned to the model; no automatic retry loop.
            if not acquisition:
                self.exhausted_sources.add(call.arguments["source_id"])
            self._set(self._searches())
            return
        bundle = observation.payload.get("research_bundle", {})
        artifacts = bundle.get("artifacts", observation.payload.get("artifacts", []))
        records = {r["source_record_id"]: r for r in bundle.get("source_records", [])}
        if acquisition:
            wanted = set(call.arguments["full_text_source_record_ids"])
            self._set([self._read(a["artifact_id"], 0, "读取已获取的全文")
                       for a in artifacts if a.get("role") == "extracted_text"
                       and a.get("source_record_id") in wanted
                       and (a["artifact_id"], 0) not in self.attempted_reads] or self._searches())
            return
        actions = []
        for artifact in artifacts:
            if artifact.get("role") != "abstract":
                continue
            aid = artifact["artifact_id"]
            record = records.get(artifact.get("source_record_id"))
            if record is None or aid in self.consumed:
                continue
            self.candidates[aid] = record
            actions.append(self._read(aid, 0, f"读取摘要：{record.get('title', aid)}"))
        if not actions:
            self.exhausted_sources.add(call.arguments["source_id"])
        self._set(actions or self._searches())

    def _after_read(self, call, observation):
        aid, start = call.arguments["artifact_id"], call.arguments.get("char_start", 0)
        self.attempted_reads.add((aid, start))
        rows = observation.payload.get("read_results") or [observation.payload.get("read_result", {})]
        row = next((r for r in rows if r.get("artifact_id") == aid and r.get("text")), None)
        if not observation.succeeded or row is None:
            self.consumed.add(aid)
            self._set(self._searches())
            return
        end = row.get("char_end", start)
        if row.get("has_more") and isinstance(end, int) and end > start:
            next_read = self._read(aid, end, "继续读取摘要剩余部分" if aid in self.candidates
                                   else "继续读取全文下一段")
            # Finish the abstract before offering full text; full text may stop at a fragment.
            self._set([next_read] + ([] if aid in self.candidates else self._searches()))
            return
        if aid in self.candidates:
            self.consumed.add(aid)
            record = self.candidates[aid]
            self.active = record
            self._set([_Action("进一步获取此文献全文", "database_search", {
                "source_id": record["source_id"],
                "full_text_source_record_ids": [record["source_record_id"]]})] + self._searches())
        else:
            self._set(self._searches())
