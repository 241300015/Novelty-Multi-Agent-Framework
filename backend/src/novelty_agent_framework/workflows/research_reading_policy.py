"""Research reading menus built from tool observations, never model-written IDs.

One instance belongs to one Harness invocation. The generic Harness only knows
how to present/resolve a policy; research phases and source handles live here.
"""
from __future__ import annotations

from ..schemas.failures import FailureCode as C, CodedFailure

import json
import uuid
from dataclasses import dataclass, replace

from backend.env import ModelToolCall, ToolDefinition


@dataclass(frozen=True)
class _Action:
    label: str
    name: str
    arguments: dict


class ResearchReadingPolicy:
    initial_instruction = (
        "Harness 自动执行首轮检索，无需你选择或确认。工具按注入配置检索已启用数据库；"
        "接下来提供实际结果。本轮无文献返回时自动推进下一轮，失败与零命中分别记录。")

    def __init__(self, registry, scope, *, max_chars=8000):
        self.registry = registry
        self.scope = scope
        self.max_chars = max_chars
        # Missing capabilities are reported by prepare_actions, never silent success.
        self.discovery_enabled = True
        self.candidates = {}
        self.metadata_candidates = {}
        self.last_round_returned = False
        self.last_round_succeeded = None
        self.phase = "discovery"
        self.automatic_action = None
        self.automatic_instruction = self.initial_instruction
        self.execution_limits = []
        self.consumed = set()
        self.attempted_reads = set()
        self.round_index = 0
        self.artifact_rounds = {}
        self.record_rounds = {}
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
        self.issued_options = set()
        self.version = 0
        self.actions = {}
        directive = getattr(scope, "recovery", None)
        action = directive.action.value if directive else None
        self.discovery_recovery = action in {"change_provider", "retry_request"}
        if directive is not None:
            # Every recovery gets one round, including a newly compiled replan.
            # Its menu and budget must authorize the same number of rounds.
            self.rounds = [plan] if plan is not None and plan.strategies else []
        if action in {"read_artifact", "fetch_fulltext"}:
            self.discovery_enabled = False
            self.search_stopped = True
        self.initial_search_pending = bool(self.rounds and self.discovery_enabled)
        if action == "read_artifact":
            self.bodies = {a.artifact_id: "读取补检指定材料" for a in directive.artifacts}
            self._next_body()
        elif action == "fetch_fulltext":
            self.recovery_fetches = [_Action("获取补检指定文献全文", "database_search", {
                "source_id": directive.source_id, "full_text_source_record_ids": [record]})
                for record in directive.source_record_ids]
            self._next_candidate()
        else:
            self._round_boundary()

    def _searches(self):
        if self.search_stopped or self.round_index >= len(self.rounds):
            return []
        levels = ", ".join(s.level for s in self.rounds[self.round_index].strategies)
        return ([_Action(f"执行第 {self.round_index + 1} 轮检索（{levels}）",
                         "database_search", {})] if self.discovery_enabled else [])

    @property
    def can_finish(self):
        return self.phase == "final" and not self.initial_search_pending and self.automatic_action is None

    @property
    def can_transition_without_tools(self):
        return self.can_finish or any(a.name in {"_skip", "_stop"} for a in self.actions.values())

    def initial_tool_call(self):
        """Compatibility entry for the required first discovery."""
        return self.automatic_tool_call() if self.initial_search_pending else None

    def automatic_tool_call(self):
        action = self.automatic_action
        if action is None:
            return None
        self.automatic_action = None
        self.initial_search_pending = False
        return ModelToolCall(id="automatic_" + uuid.uuid4().hex,
                             name=action.name, arguments=dict(action.arguments))

    def _automatic(self, action, instruction):
        self._set([])
        self.automatic_action = action
        self.automatic_instruction = instruction

    def prepare_actions(self, prepare):
        """Reconcile program-owned actions with capabilities and current budgets.

        prepare returns (adjusted_call, None, None), or (None, code, reason).
        No model is asked to fix injected arguments or unavailable operations.
        """
        notices = []
        while True:
            proposed = ([('automatic', self.automatic_action)] if self.automatic_action
                        else list(self.actions.items()))
            blocked = []
            for key, action in proposed:
                if action.name in {"_skip", "_stop"}:
                    continue
                call = ModelToolCall(id="availability", name=action.name, arguments=dict(action.arguments))
                adjusted, code, reason = prepare(call)
                if adjusted is not None:
                    updated = replace(action, arguments=dict(adjusted.arguments))
                    if key == 'automatic':
                        self.automatic_action = updated
                    else:
                        self.actions[key] = updated
                    continue
                notice = {"code": code, "reason": reason, "tool": action.name,
                          "arguments": dict(action.arguments), "phase": self.phase,
                          "required": key == 'automatic' or self.phase in {'abstracts', 'bodies', 'continuation', 'recovery_acquisition'}}
                if notice not in self.execution_limits:
                    self.execution_limits.append(notice)
                    notices.append(notice)
                blocked.append(action)
                if action.name == 'reader':
                    self.consumed.add(action.arguments['artifact_id'])
                    self.bodies.pop(action.arguments['artifact_id'], None)
                if key == 'automatic':
                    self.automatic_action = None
                    self.initial_search_pending = False
                else:
                    del self.actions[key]
            # A forced choice between a disabled acquisition/search and skip/stop
            # becomes a program transition, with the limitation explicitly kept.
            if blocked and self.actions and all(a.name in {"_skip", "_stop"} for a in self.actions.values()):
                action = next(iter(self.actions.values()))
                if action.name == '_stop':
                    self.search_stopped = True
                    self._next_body()
                else:
                    self._next_candidate()
                continue
            if self.actions or self.automatic_action or self.can_finish:
                return notices
            if not blocked:
                raise ValueError("Research phase has no action and is not completed")
            for action in blocked:
                if action.name == 'database_search' and not action.arguments.get('full_text_source_record_ids'):
                    self.search_stopped = True
            self._next_candidate()

    @property
    def budget_scope_count(self):
        # Recovery and missing strategy levels must not grant three rounds.
        if getattr(self.scope, "recovery", None) is not None:
            return 1
        return max(1, len(self.rounds))

    def budget_scope(self, call):
        """Charge deferred body reads to the round that selected the source."""
        if call.name == "database_search":
            records = call.arguments.get("full_text_source_record_ids")
            if not records:
                return self.round_index + 1
            return self.record_rounds.get(
                (call.arguments.get("source_id"), records[0]), max(1, self.round_index))
        if call.name == "reader":
            return self.artifact_rounds.get(call.arguments.get("artifact_id"),
                                            max(1, self.round_index))
        return max(1, self.round_index)

    def execution_scope(self, call):
        if call.name == "database_search" and not call.arguments.get("full_text_source_record_ids"):
            if not self._searches():
                raise ValueError("no remaining search strategies; discovery is stopped")
            if self.discovery_recovery:
                return self.scope
            return self.scope.model_copy(update={"search_plan": self.rounds[self.round_index]})
        return self.scope

    def _round_boundary(self):
        self.phase = "discovery"
        searches = self._searches()
        if searches and (self.initial_search_pending or not self.last_round_returned):
            notice = (self.initial_instruction if self.initial_search_pending else
                f"Harness 自动启动第 {self.round_index + 1} 轮检索，无需选择或确认。"
                + ("上一轮检索失败；保留失败记录，不视为成功零命中。"
                   if self.last_round_succeeded is False else "上一轮未返回文献。"))
            self._automatic(searches[0], notice)
        elif searches:
            label = "停止检索，开始阅读已选全文" if self.bodies else "停止检索，按已有材料提交结果"
            self._set(searches + [_Action(label, "_stop", {})])
        else:
            self.initial_search_pending = False
            self.search_stopped = True
            self._next_body()

    def _next_candidate(self):
        if self.recovery_fetches:
            self.phase = "recovery_acquisition"
            self._automatic(self.recovery_fetches.pop(0), "Harness 执行已指定的全文获取，无需再次选择。")
            return
        if self.search_stopped:
            self._next_body()
            return
        unread = [self._read(aid, 0, f"读取摘要：{record.get('title', aid)}")
                  for aid, record in self.candidates.items() if aid not in self.consumed]
        if unread:
            self.phase = "abstracts"
            self._set(unread)
        elif self.metadata_candidates:
            _, record = self.metadata_candidates.popitem()
            self._offer_acquisition(record, metadata_only=True)
        else:
            self._round_boundary()

    def _next_body(self):
        if self.bodies:
            self.phase = "bodies"
            aid, label = next(iter(self.bodies.items()))
            self._automatic(self._read(aid, 0, label), "Harness 开始读取已选全文，无需再次选择。")
        else:
            self.phase = "final"
            self._set([])

    def _read(self, artifact_id, start, label):
        return _Action(label, "reader", {"artifact_id": artifact_id,
                       "char_start": start, "max_chars": self.max_chars})

    def _set(self, actions):
        self.automatic_action = None
        self.version += 1
        self.actions = {f"v{self.version}_{i}": action for i, action in enumerate(actions)}

    def choices(self):
        self.issued_options.update(self.actions)
        return {key: action.label for key, action in self.actions.items()}

    def selection(self, arguments, call_id):
        return ModelToolCall(id=call_id, name="choose_research_action", arguments=arguments)

    def definitions(self):
        if not self.actions:
            return ()
        return (ToolDefinition(name="choose_research_action", description=(
            "选择当前允许的下一步；Harness 执行工具并填写可信索引，完成动作阶段后另行通知生成结果。选项：" +
            json.dumps(self.choices(), ensure_ascii=False)),
            parameters={"type": "object", "properties": {
                "option_id": {"type": "string", "enum": list(self.actions)}},
                "required": ["option_id"], "additionalProperties": False}),)

    def resolve(self, call):
        if call.name != "choose_research_action" or set(call.arguments) != {"option_id"}:
            raise CodedFailure(C.HARNESS_DECISION_SCHEMA, "select only option_id from the current action menu")
        option = call.arguments["option_id"]
        if not isinstance(option, str) or option not in self.actions:
            code = C.HARNESS_OPTION_UNKNOWN
            if not isinstance(option, str):
                code = C.HARNESS_DECISION_SCHEMA
            elif option in self.choices().values():
                code = C.HARNESS_OPTION_LABEL
            elif option in self.issued_options:
                code = C.HARNESS_OPTION_STALE
            raise CodedFailure(code, "unknown or stale option_id; select from the current menu")
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
        budget_round = self.budget_scope(call)
        if not acquisition:
            self.initial_search_pending = False
            # Each attempted discovery consumes a strategy subset, including errors
            # and zero/newless results. Providers keep their own retry/fallback logic.
            self.round_index += 1
        bundle = observation.payload.get("research_bundle", {})
        artifacts = bundle.get("artifacts", observation.payload.get("artifacts", []))
        records = {r["source_record_id"]: r for r in bundle.get("source_records", [])}
        if not acquisition:
            self.last_round_succeeded = observation.succeeded
            self.last_round_returned = bool(records or bundle.get("works") or observation.payload.get("results")
                or any(row.get("results") for row in observation.payload.get("search_executions", [])))
            abstract_records = {a.get("source_record_id") for a in artifacts if a.get("role") == "abstract"}
            for rid, record in records.items():
                if rid not in abstract_records and (record["source_id"], rid) not in self.record_rounds:
                    self.metadata_candidates[rid] = record
                    self.record_rounds[(record['source_id'], rid)] = budget_round
        if acquisition:
            wanted = set(call.arguments["full_text_source_record_ids"])
            for artifact in artifacts:
                aid = artifact.get("artifact_id")
                if (aid and artifact.get("role") == "extracted_text"
                        and artifact.get("source_record_id") in wanted
                        and (aid, 0) not in self.attempted_reads):
                    self.bodies[aid] = "读取已选文献全文"
                    self.artifact_rounds.setdefault(aid, budget_round)
        else:
            for artifact in artifacts:
                if artifact.get("role") != "abstract":
                    continue
                aid = artifact["artifact_id"]
                record = records.get(artifact.get("source_record_id"))
                if record is not None and aid not in self.consumed:
                    self.candidates[aid] = record
                    self.artifact_rounds.setdefault(aid, budget_round)
                    self.record_rounds.setdefault(
                        (record["source_id"], record["source_record_id"]), budget_round)
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
            self.phase = "continuation"
            self._automatic(self._read(aid, end, "继续读取下一段"),
                            "Harness 继续读取当前材料的下一段，无需再次选择。")
            return
        if aid in self.candidates:
            self.consumed.add(aid)
            self._offer_acquisition(self.candidates[aid])
        else:
            self.bodies.pop(aid, None)
            self._next_body()

    def _offer_acquisition(self, record, *, metadata_only=False):
        self.phase = "acquisition"
        self.active = record
        if self.discovery_recovery:
            self._next_candidate()
            return
        label = "获取此文献全文，留待停止检索后阅读"
        if metadata_only:
            label += f"（{record.get('title', '已返回文献')}：暂无摘要材料）"
        self._set([_Action(label, "database_search", {
            "source_id": record["source_id"],
            "full_text_source_record_ids": [record["source_record_id"]]}),
            _Action("不获取此文献全文，继续处理本轮材料", "_skip", {})])
