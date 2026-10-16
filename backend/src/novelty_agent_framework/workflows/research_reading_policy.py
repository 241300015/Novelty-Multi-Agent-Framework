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
        self.round_index = 0
        self.rounds = []
        plan = getattr(scope, "search_plan", None)
        if plan is not None:
            # Keep Planner's strategies intact; only remove levels, never compile
            # or interpret provider results/fallbacks here. Skip duplicate/empty subsets.
            for removed in (set(), {"strict"}, {"strict", "medium"}):
                strategies = [s for s in plan.strategies if s.level not in removed]
                if strategies and not any(strategies == r.strategies for r in self.rounds):
                    self.rounds.append(plan.model_copy(update={"strategies": strategies}))
        self.bodies = {}
        self.search_stopped = False
        self.recovery_fetches = []
        self.active = None
        self.version = 0
        self.actions = {}
        directive = getattr(scope, "recovery", None)
        action = directive.action.value if directive else None
        self.discovery_recovery = action in {"change_provider", "retry_request"}
        if self.discovery_recovery:
            # A recovery directive authorizes exactly its frozen request, not a
            # new sequence of plans. Keep RecoveryToolRegistry's scope invariant.
            self.rounds = [plan] if plan is not None and plan.strategies else []
        if action in {"read_artifact", "fetch_fulltext"}:
            self.sources = ()
            self.search_stopped = True
        if action == "read_artifact":
            self.bodies = {a.artifact_id: "读取补检指定材料" for a in directive.artifacts}
            self._next_body()
        elif action == "fetch_fulltext":
            self.recovery_fetches = [_Action("获取补检指定文献全文", "database_search", {
                "source_id": directive.source_id, "full_text_source_record_ids": [record]})
                for record in directive.source_record_ids]
            self._next_candidate()
        else:
            if directive and directive.source_id:
                self.sources = tuple(s for s in self.sources if s == directive.source_id)
            self._round_boundary()

    def _searches(self):
        if self.search_stopped or self.round_index >= len(self.rounds):
            return []
        levels = ", ".join(s.level for s in self.rounds[self.round_index].strategies)
        return [_Action(f"执行第 {self.round_index + 1} 轮检索：{source}（{levels}）",
                        "database_search", {"source_id": source}) for source in self.sources]

    @property
    def can_finish(self):
        return not self.actions

    def execution_scope(self, call):
        if call.name == "database_search" and not call.arguments.get("full_text_source_record_ids"):
            if not self._searches():
                raise ValueError("no remaining search strategies; discovery is stopped")
            if self.discovery_recovery:
                return self.scope
            return self.scope.model_copy(update={"search_plan": self.rounds[self.round_index]})
        return self.scope

    def _round_boundary(self):
        searches = self._searches()
        if searches:
            self._set(searches + [_Action("停止检索，开始阅读已选全文", "_stop", {})])
        else:
            # Empty strategy sets never reach the registry or consume a model choice.
            self.search_stopped = True
            self._next_body()

    def _next_candidate(self):
        if self.recovery_fetches:
            self._set([self.recovery_fetches.pop(0)])
            return
        if self.search_stopped:
            self._next_body()
            return
        unread = [self._read(aid, 0, f"读取摘要：{record.get('title', aid)}")
                  for aid, record in self.candidates.items() if aid not in self.consumed]
        if unread:
            self._set(unread)
        else:
            self._round_boundary()

    def _next_body(self):
        self._set([self._read(aid, 0, label) for aid, label in list(self.bodies.items())[:1]])

    def _read(self, artifact_id, start, label):
        return _Action(label, "reader", {"artifact_id": artifact_id,
                       "char_start": start, "max_chars": self.max_chars})

    def _set(self, actions):
        self.version += 1
        self.actions = {f"v{self.version}_{i}": action for i, action in enumerate(actions)
                        if action.name in self.registry.names or action.name in {"_skip", "_stop"}}

    def choices(self):
        return {key: action.label for key, action in self.actions.items()}

    def selection(self, arguments, call_id):
        return ModelToolCall(id=call_id, name="choose_research_action", arguments=arguments)

    def definitions(self):
        if not self.actions:
            return ()
        return (ToolDefinition(name="choose_research_action", description=(
            "选择当前允许的下一步；Harness 执行工具并填写可信索引。菜单为空后提交最终 JSON。选项：" +
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
        if action.name == "_skip":
            self._next_candidate()
            return None
        if action.name == "_stop":
            self.search_stopped = True
            self._next_body()
            return None
        resolved = ModelToolCall(id=call.id, name=action.name, arguments=dict(action.arguments))
        self.execution_scope(resolved)  # Validate the remaining plan before any I/O.
        return resolved

    def observe(self, call, observation):
        if call.name == "database_search":
            self._after_database(call, observation)
        elif call.name == "reader":
            self._after_read(call, observation)

    def _after_database(self, call, observation):
        acquisition = bool(call.arguments.get("full_text_source_record_ids"))
        if not acquisition:
            # Each attempted discovery consumes a strategy subset, including errors
            # and zero/newless results. Providers keep their own retry/fallback logic.
            self.round_index += 1
        if not observation.succeeded:
            self._next_candidate()
            return
        bundle = observation.payload.get("research_bundle", {})
        artifacts = bundle.get("artifacts", observation.payload.get("artifacts", []))
        records = {r["source_record_id"]: r for r in bundle.get("source_records", [])}
        if acquisition:
            wanted = set(call.arguments["full_text_source_record_ids"])
            for artifact in artifacts:
                aid = artifact.get("artifact_id")
                if (aid and artifact.get("role") == "extracted_text"
                        and artifact.get("source_record_id") in wanted
                        and (aid, 0) not in self.attempted_reads):
                    self.bodies[aid] = "读取已选文献全文"
        else:
            for artifact in artifacts:
                if artifact.get("role") != "abstract":
                    continue
                aid = artifact["artifact_id"]
                record = records.get(artifact.get("source_record_id"))
                if record is not None and aid not in self.consumed:
                    self.candidates[aid] = record
        self._next_candidate()

    def _after_read(self, call, observation):
        aid, start = call.arguments["artifact_id"], call.arguments.get("char_start", 0)
        self.attempted_reads.add((aid, start))
        rows = observation.payload.get("read_results") or [observation.payload.get("read_result", {})]
        row = next((r for r in rows if r.get("artifact_id") == aid and r.get("text")), None)
        if not observation.succeeded or row is None:
            self.consumed.add(aid)
            self.bodies.pop(aid, None)
            self._next_candidate()
            return
        end = row.get("char_end", start)
        if row.get("has_more") and isinstance(end, int) and end > start:
            self._set([self._read(aid, end, "继续读取摘要剩余部分" if aid in self.candidates
                                 else "继续读取全文下一段")])
            return
        if aid in self.candidates:
            self.consumed.add(aid)
            record = self.candidates[aid]
            self.active = record
            acquisitions = [] if self.discovery_recovery else [_Action("获取此文献全文，留待停止检索后阅读", "database_search", {
                "source_id": record["source_id"],
                "full_text_source_record_ids": [record["source_record_id"]]})]
            self._set(acquisitions + [_Action("不获取此文献全文，继续处理本轮摘要", "_skip", {})])
        else:
            self.bodies.pop(aid, None)
            self._next_body()
