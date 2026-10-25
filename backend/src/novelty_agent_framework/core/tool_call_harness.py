"""Serial model tool-calling loop over the Researcher tool registry.

This module will bridge:
- ModelClient native tool-calling protocol
- task-scoped tool registry definitions and execution
- assistant/tool message trajectory

The first implementation is serial:
- 0 tool calls -> finish
- 1 tool call -> execute and continue
- >1 tool calls -> retain and execute only the first call

Business tool implementations do not belong here.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Literal, Protocol

from backend.env import (
    ChatMessage,
    ModelCallOptions,
    ModelClient,
    ModelResponse,
    ModelToolCall,
    ToolDefinition,
)

from ..schemas import ResearcherToolObservation
from ..schemas.failures import FailureCode as C, CodedFailure
from .failure_classification import model_failure_code
from .runtime_artifacts import current_runtime_artifacts

HarnessEventKind = Literal[
    "initial_user_message",
    "assistant_response",
    "tool_call",
    "tool_result",
    "error",
    "finish",
]


@dataclass(frozen=True)
class ToolCallHarnessEvent:
    """One immutable fact appended during a harness run."""

    kind: HarnessEventKind
    message: ChatMessage | None = None
    tool_call: ModelToolCall | None = None
    observation: ResearcherToolObservation | None = None
    detail: str | None = None
    failure_code: C | None = None


class ToolCallHarnessError(RuntimeError):
    """Serial policy, model, or budget failure with the trace so far."""

    def __init__(
        self, message: str, *, trace: tuple[ToolCallHarnessEvent, ...] = ()
    ) -> None:
        super().__init__(message)
        self.trace = trace


class ToolCallBudgetExhausted(ToolCallHarnessError):
    """A budget boundary eligible for one tool-free finalization."""

    def __init__(self, message: str, *, trace=(), runtime_state=None):
        super().__init__(message, trace=trace)
        self.runtime_state = runtime_state


@dataclass(frozen=True)
class ToolCallHarnessConfig:
    round_budgets: bool = False
    text_choices: bool = False
    finalize_on_budget: bool = False
    reuse_database_results: bool = False
    reuse_reader_results: bool = False
    runtime_state_projection: bool = False
    max_turns: int = 12
    max_tool_calls: int = 10
    per_tool_limits: dict[str, int] = field(default_factory=dict)
    max_total_read_chars: int | None = None
    finalization_instruction: str | None = None
    reserve_final_turn: bool = False
    report_budget: bool = False
    allow_one_read_budget_correction: bool = False

    def __post_init__(self) -> None:
        if self.max_turns < 1:
            raise ValueError("max_turns must be positive")
        if self.max_tool_calls < 1:
            raise ValueError("max_tool_calls must be positive")
        if any(value < 1 for value in self.per_tool_limits.values()):
            raise ValueError("per_tool_limits must be positive")
        if self.max_total_read_chars is not None and self.max_total_read_chars < 1:
            raise ValueError("max_total_read_chars must be positive")


@dataclass(frozen=True)
class ToolCallHarnessResult:
    final_content: str | None
    trace: tuple[ToolCallHarnessEvent, ...]
    tool_calls_used: int
    turns_used: int
    stop_reason: str | None = None


class ToolActionPolicy(Protocol):
    """Invocation-local menu policy; execution and permissions remain in the registry.

    resolve may advance a local stage and return None (no tool budget charge).
    Policies may expose execution_scope(call) for runtime-owned plan selection
    and can_finish to guard pending stages; forced budget finalization still applies.
    With round_budgets enabled, budget_scope_count and budget_scope(call) assign
    independent per-tool quotas. Task-wide safety ceilings scale with scope count.
    """

    def definitions(self) -> tuple[ToolDefinition, ...]: ...
    def resolve(self, call: ModelToolCall) -> ModelToolCall | None: ...
    def choices(self) -> dict[str, str]: ...
    def selection(self, arguments: dict, call_id: str) -> ModelToolCall: ...
    def observe(self, call: ModelToolCall, observation: ResearcherToolObservation) -> None: ...


class ToolCallHarness:
    """Run a model until it finishes, executing at most one tool per turn."""

    def __init__(
        self,
        model_client: ModelClient,
        registry: Any,
        *,
        config: ToolCallHarnessConfig | None = None,
        action_policy_factory: Callable[[Any, Any], ToolActionPolicy] | None = None,
    ) -> None:
        self.model_client = model_client
        self.registry = registry
        self.config = config or ToolCallHarnessConfig()
        self.action_policy_factory = action_policy_factory
        if self.config.text_choices and action_policy_factory is None:
            raise ValueError("text_choices requires an action policy")

    def _tool_message(self, *, role="tool", tool_call_id, content):
        if self.config.text_choices:
            # Text decisions are ordinary assistant messages, so there must be no
            # synthetic tool-role reply on the provider wire.
            return ChatMessage(role="user", content="Harness observation:\n" + content)
        return ChatMessage(role=role, tool_call_id=tool_call_id, content=content)

    async def run(
        self, *, system_prompt: str, initial_user_message: str,
        scope: Any, options: ModelCallOptions | None = None,
    ) -> ToolCallHarnessResult:
        log: list[ToolCallHarnessEvent] = []
        try:
            return await self._run(
                system_prompt=system_prompt, initial_user_message=initial_user_message,
                scope=scope, options=options, log=log,
            )
        except ToolCallBudgetExhausted as exc:
            if not self.config.finalize_on_budget:
                raise
            return await self._finalize(system_prompt, exc, options)
        except ToolCallHarnessError:
            raise
        except Exception as exc:
            # Policy/bookkeeping failures must not discard completed tool work.
            # BaseException (cancellation or refused model switching) still aborts.
            detail = f"Harness failed: {type(exc).__name__}: {exc}"
            _append_error(log, detail, C.UNKNOWN)
            raise ToolCallHarnessError(detail, trace=tuple(log)) from exc

    async def _finalize(self, system_prompt, error, options):
        # Complete any outstanding assistant/tool pair before appending a user turn.
        log = list(error.trace)
        pending = {}
        for event in log:
            message = event.message
            if message is None:
                continue
            for call in message.tool_calls or ():
                pending[call.id] = call
            if message.role == "tool":
                pending.pop(message.tool_call_id, None)
        for call in pending.values():
            log.append(ToolCallHarnessEvent(kind="tool_result", message=self._tool_message(
                role="tool", tool_call_id=call.id,
                content=json.dumps({"succeeded": False, "error": str(error)}),
            )))
        log.append(ToolCallHarnessEvent(kind="initial_user_message", message=ChatMessage(
            role="user", content=(self.config.finalization_instruction or (
                f"Research stopped: {error}. No further tool calls are allowed. "
                "This is the single reserved finalization turn. Return the required "
                "final JSON using only observations already received. Preserve exact "
                "Reader quotes, including mathematical markup. If no text supports "
                "evidence, return cards=[] with a concrete no_evidence_reason."
            )) + f"\nStop reason: {error}",
        )))
        try:
            context = _build_context(system_prompt, tuple(log))
            if self.config.runtime_state_projection and error.runtime_state is not None:
                context = _with_runtime_state(context, error.runtime_state)
            response = await self.model_client.acomplete(
                context,
                options=replace(options or ModelCallOptions(), tools=(), tool_choice="none"),
            )
            if response.tool_calls:
                raise CodedFailure(C.HARNESS_NATIVE_CALL, "finalization attempted a tool call")
        except Exception as exc:
            _append_error(log, f"budget finalization failed: {type(exc).__name__}: {exc}", model_failure_code(exc))
            raise ToolCallHarnessError("budget finalization failed", trace=tuple(log)) from exc
        log.append(ToolCallHarnessEvent(kind="assistant_response", message=ChatMessage(
            role="assistant", content=response.content,
        )))
        log.append(ToolCallHarnessEvent(kind="finish", detail="budget finalization"))
        return ToolCallHarnessResult(
            final_content=response.content, trace=tuple(log),
            tool_calls_used=sum(e.kind == "tool_call" for e in log),
            turns_used=sum(e.kind == "assistant_response" for e in log),
            stop_reason=str(error),
        )

    async def _run(
        self, *, system_prompt: str, initial_user_message: str,
        scope: Any, log: list[ToolCallHarnessEvent], options: ModelCallOptions | None = None,
    ) -> ToolCallHarnessResult:
        log.append(
            ToolCallHarnessEvent(
                kind="initial_user_message",
                message=ChatMessage(role="user", content=initial_user_message),
            )
        )
        policy = self.action_policy_factory(self.registry, scope) if self.action_policy_factory else None
        scoped_budgets = self.config.round_budgets and policy is not None
        scope_count = policy.budget_scope_count if scoped_budgets else 1
        config = replace(self.config,
            max_turns=(self.config.max_turns - int(self.config.reserve_final_turn)) * scope_count
                      + int(self.config.reserve_final_turn),
            max_tool_calls=self.config.max_tool_calls * scope_count,
            max_total_read_chars=(None if self.config.max_total_read_chars is None
                                  else self.config.max_total_read_chars * scope_count),
            per_tool_limits={name: limit * scope_count
                             for name, limit in self.config.per_tool_limits.items()})
        tool_definitions = _build_tool_definitions(self.registry)
        call_options = replace(options or ModelCallOptions(), tools=tool_definitions)
        tool_calls_used = 0
        per_tool_counts: dict[str, int] = {}
        scoped_tool_counts: dict[int, dict[str, int]] = {}
        total_read_chars = 0
        read_budget_rejections = 0
        required_reader_artifact_ids: set[str] = set()
        database_results = {}  # Per invocation: never crosses task/plan/run scopes.
        reader_state = _ReaderState()
        invocation_id = uuid.uuid4().hex

        def scope_budgets():
            return {str(index): {name: max(0, limit - scoped_tool_counts.get(index, {}).get(name, 0))
                                for name, limit in self.config.per_tool_limits.items()}
                    for index in range(1, scope_count + 1)}

        def projected_state(*, remaining_turns, phase="exploration", stop_reason=None):
            state = _project_runtime_state(config, tuple(log), scope=scope,
                invocation_id=invocation_id, phase=phase, stop_reason=stop_reason,
                remaining_turns=remaining_turns, tool_calls_used=tool_calls_used,
                per_tool_counts=per_tool_counts, total_read_chars=total_read_chars,
                required_reader_artifact_ids=required_reader_artifact_ids,
                deadline=getattr(self.model_client, "deadline", None))
            if scoped_budgets:
                state["budget"]["remaining_per_tool_by_round"] = scope_budgets()
            return state

        def budget_error(detail, code):
            _record_budget_stop(
                scope=scope,
                code=code,
                detail=detail,
                occurrence_id=f"{invocation_id}:{len(log)}",
                budget={
                    "local_limit": config.max_tool_calls,
                    "local_used": tool_calls_used,
                },
            )
            return ToolCallBudgetExhausted(detail, trace=tuple(log), runtime_state=(
                projected_state(remaining_turns=0, phase="finalization", stop_reason=detail)
                if self.config.runtime_state_projection else None))

        exploration_turns = config.max_turns - int(self.config.reserve_final_turn)
        if exploration_turns < 1:
            raise ValueError("reserved final turn requires at least two total turns")
        for turn in range(1, exploration_turns + 1):
            if self.config.finalize_on_budget and self.config.reserve_final_turn \
                    and tool_calls_used >= config.max_tool_calls:
                _append_error(log, "tool-call exploration limit reached", C.BUDGET_TOOLS)
                raise budget_error("tool-call exploration limit reached", C.BUDGET_TOOLS)
            context = _build_context(system_prompt, tuple(log))
            if self.config.runtime_state_projection:
                context = _with_runtime_state(context,
                    projected_state(remaining_turns=exploration_turns - turn + 1))
            if not self.config.runtime_state_projection and self.config.reuse_reader_results and reader_state.reads:
                context[0] = replace(context[0], content=context[0].content +
                    "\nReader state (verified observations in this task): " +
                    json.dumps(reader_state.project(), ensure_ascii=False))
            if not self.config.runtime_state_projection and (self.config.finalize_on_budget or self.config.report_budget):
                context[0] = replace(context[0], content=context[0].content + "\nBudget context: " + json.dumps({
                    "remaining_research_turns": exploration_turns - turn + 1,
                    "remaining_exploration_turns": exploration_turns - turn + 1,
                    "remaining_tool_calls": config.max_tool_calls - tool_calls_used,
                    "remaining_read_chars": (None if config.max_total_read_chars is None
                                             else config.max_total_read_chars - total_read_chars),
                    "remaining_exploration_seconds": (
                        max(0, round(self.model_client.deadline - time.monotonic(), 1))
                        if isinstance(getattr(self.model_client, "deadline", None), (int, float)) else None),
                    "finalization_reserved": self.config.finalize_on_budget and self.config.reserve_final_turn,
                    "remaining_per_tool": {
                        name: max(0, limit - per_tool_counts.get(name, 0))
                        for name, limit in config.per_tool_limits.items() if not self.config.text_choices
                    },
                    **({"remaining_per_tool_by_round": scope_budgets()} if scoped_budgets else {}),
                    "instruction": "Finish within the remaining exploration budget; a reserved tool-free final turn follows if needed.",
                }))
            if policy is not None:
                if self.config.text_choices:
                    choices = policy.choices()
                    call_options = decision_options(options or ModelCallOptions(), choices)
                    context.append(ChatMessage(role="user", content=(
                        "Current Harness choices (only these option_id values are valid):\n" +
                        json.dumps(choices, ensure_ascii=False) +
                        '\nReturn {"option_id":"..."} to select one option, or return the required '
                        'final draft JSON when the choices are empty. Do not call functions or supply execution arguments. '
                        'If the choices are empty, finish with the evidence already observed.')))
                else:
                    definitions = policy.definitions()
                    call_options = replace(call_options, tools=definitions,
                                           tool_choice="auto" if definitions else "none")
                    context[0] = replace(context[0], content=context[0].content +
                        "\nUse only the current tool definitions and their allowed options. The runtime owns all tool "
                        "arguments. Earlier tool names/options are not callable now. You may finish "
                        "with the required JSON when the menu is empty; complete pending stages first.")
            try:
                response = await self.model_client.acomplete(
                    context, options=call_options
                )
            except Exception as exc:
                _append_error(log, f"model call failed: {type(exc).__name__}: {exc}", model_failure_code(exc))
                raise ToolCallHarnessError(
                    "model call failed", trace=tuple(log)
                ) from exc

            if self.config.text_choices:
                if response.tool_calls:
                    # Even a server/model ignoring tool_choice=none cannot execute a tool.
                    log.append(ToolCallHarnessEvent(kind="assistant_response", message=ChatMessage(
                        role="assistant", content=response.content or "[unexpected function call omitted]")))
                    _append_error(log, "action policy rejected selection: native tool calls are disabled", C.HARNESS_NATIVE_CALL)
                    log.append(ToolCallHarnessEvent(kind="tool_result", message=ChatMessage(role="user",
                        content='Harness rejected the function call. Return {"option_id":"..."} or final draft JSON.')))
                    continue
                try:
                    decision = json.loads(response.content or "")
                except (ValueError, TypeError):
                    decision = None
                if isinstance(decision, dict) and "option_id" in decision:
                    response = replace(response, tool_calls=(policy.selection(decision, f"choice_{turn}"),))

            if not response.tool_calls and policy is not None and not getattr(policy, "can_finish", True):
                log.append(ToolCallHarnessEvent(kind="assistant_response", message=ChatMessage(
                    role="assistant", content=response.content)))
                _append_error(log, "action policy rejected early finish: pending research stages",
                    C.HARNESS_DECISION_SCHEMA if self.config.text_choices and decision is None else C.HARNESS_EARLY_FINISH)
                log.append(ToolCallHarnessEvent(kind="tool_result", message=ChatMessage(role="user",
                    content=getattr(policy, "pending_instruction", "Research stages remain. Select a current option; stop discovery at the round boundary, then read selected full texts before submitting the draft."))))
                continue
            if not response.tool_calls:
                assistant_message = ChatMessage(
                    role="assistant",
                    content=response.content,
                )
                log.append(
                    ToolCallHarnessEvent(
                        kind="assistant_response", message=assistant_message
                    )
                )
                log.append(
                    ToolCallHarnessEvent(kind="finish", detail="model finished")
                )
                return ToolCallHarnessResult(
                    final_content=response.content,
                    trace=tuple(log),
                    tool_calls_used=tool_calls_used,
                    turns_used=turn,
                )

            original_tool_calls = tuple(response.tool_calls)
            dropped_tool_calls = original_tool_calls[1:]
            if dropped_tool_calls:
                _append_error(
                    log,
                    json.dumps(
                        {
                            "policy": "SERIAL_FIRST_CALL",
                            "selected_tool_call_id": original_tool_calls[0].id,
                            "dropped_tool_call_ids": [
                                call.id for call in dropped_tool_calls
                            ],
                        },
                        sort_keys=True,
                    ),
                    C.HARNESS_MULTIPLE_CALLS,
                )
                response = ModelResponse(
                    content=response.content,
                    tool_calls=original_tool_calls[:1],
                    raw=response.raw,
                    usage=response.usage,
                )

            assistant_message = ChatMessage(
                role="assistant",
                content=response.content,
                tool_calls=() if self.config.text_choices else tuple(response.tool_calls),
            )
            log.append(
                ToolCallHarnessEvent(
                    kind="assistant_response", message=assistant_message
                )
            )

            selected_call = response.tool_calls[0]
            tool_call = selected_call
            if policy is not None:
                try:
                    tool_call = policy.resolve(selected_call)
                except ValueError as exc:
                    # No I/O; bounded by model turns, not charged as a tool execution.
                    _append_error(log, f"action policy rejected selection: {exc}",
                                  getattr(exc, "code", C.HARNESS_DECISION_SCHEMA))
                    log.append(ToolCallHarnessEvent(kind="tool_result", tool_call=selected_call,
                        message=self._tool_message(role="tool", tool_call_id=selected_call.id,
                            content=json.dumps({"succeeded": False, "error": str(exc)}))))
                    continue
            if tool_call is None:
                log.append(ToolCallHarnessEvent(kind="tool_result", tool_call=selected_call,
                    detail="policy_transition", message=self._tool_message(role="tool",
                        tool_call_id=selected_call.id,
                        content='{"succeeded":true,"instruction":"Harness advanced the stage; no tool was executed. Select from the updated menu."}')))
                continue
            execution_scope = (policy.execution_scope(tool_call)
                               if policy is not None and hasattr(policy, "execution_scope") else scope)
            try:
                validated_arguments = self.registry.validate_arguments(
                    tool_call.name, tool_call.arguments
                )
            except Exception:
                validated_arguments = None
            runtime = current_runtime_artifacts()
            resolved_arguments = (
                validated_arguments.model_dump(mode="json")
                if validated_arguments is not None
                else dict(tool_call.arguments)
            )
            runtime_call = (
                runtime.start_tool_call(
                    tool_call.name,
                    agent_arguments=dict(selected_call.arguments),
                    resolved_arguments=resolved_arguments,
                    agent_tool_call_id=tool_call.id,
                )
                if runtime is not None
                else None
            )

            # A replay is a model action, not new I/O or new evidence. max_turns
            # still bounds repetitive models. EOF applies only at/after the
            # known artifact end, so earlier unread ranges remain available.
            replay = (reader_state.replay(resolved_arguments)
                      if self.config.reuse_reader_results and tool_call.name == "reader"
                      and validated_arguments is not None
                      and (not required_reader_artifact_ids or
                           _reader_artifact_ids(resolved_arguments).intersection(required_reader_artifact_ids))
                      else None)
            if replay is not None:
                replay_reads = replay.get("read_results", [replay.get("read_result", {})])
                required_reader_artifact_ids.difference_update(
                    row.get("artifact_id") for row in replay_reads if row.get("text")
                )
                if runtime is not None and runtime_call is not None:
                    runtime.finish_tool_call(runtime_call, raw_result=None,
                        normalized_result=replay, succeeded=True, error=None)
                log.append(ToolCallHarnessEvent(kind="tool_result", tool_call=tool_call,
                    detail="reader_state_reused", message=self._tool_message(role="tool",
                        tool_call_id=tool_call.id, content=_serialize_tool_result(replay))))
                continue

            if tool_calls_used >= config.max_tool_calls:
                detail = "total tool-call budget exhausted"
                _append_error(log, detail, C.BUDGET_TOOLS)
                if runtime is not None and runtime_call is not None:
                    runtime.finish_tool_call(
                        runtime_call,
                        raw_result=None,
                        normalized_result=None,
                        succeeded=False,
                        error={"type": "HarnessPolicyError", "message": detail},
                        failure_phase="PRE_TOOL",
                    )
                raise budget_error(detail, C.BUDGET_TOOLS)

            if required_reader_artifact_ids and policy is None:
                requested_artifacts = _reader_artifact_ids(resolved_arguments)
                if (
                    tool_call.name != "reader"
                    or not requested_artifacts.intersection(required_reader_artifact_ids)
                ):
                    # 软性拒绝：不杀死任务，向模型回传拒绝消息（含必须读取的
                    # artifact_id），让它下一轮自我纠正；审计日志保留完整记录。
                    #
                    # 这里**不**递增 tool_calls_used：本次什么都没执行，却要占掉
                    # 一个调用名额，实测一个 16 次预算的任务里被白吃掉 5 次（31%）。
                    # 循环本身仍受 max_turns 约束，不存在无限拒绝的风险。
                    detail = (
                        "reader required after database_search returned artifact_ids"
                    )
                    _append_error(log, detail, C.HARNESS_READ_REQUIRED)
                    rejection = self._tool_message(
                        role="tool",
                        tool_call_id=tool_call.id,
                        content=json.dumps(
                            {
                                "succeeded": False,
                                "summary": detail,
                                "required_artifact_ids": sorted(
                                    required_reader_artifact_ids
                                ),
                            },
                            ensure_ascii=False,
                            sort_keys=True,
                        ),
                    )
                    log.append(
                        ToolCallHarnessEvent(
                            kind="tool_result",
                            message=rejection,
                            tool_call=tool_call,
                        )
                    )
                    if runtime is not None and runtime_call is not None:
                        runtime.finish_tool_call(
                            runtime_call,
                            raw_result=None,
                            normalized_result=json.loads(rejection.content),
                            succeeded=False,
                            error={"type": "HarnessPolicyError", "message": detail},
                            failure_phase="PRE_TOOL",
                        )
                    continue
            budget_scope = policy.budget_scope(tool_call) if scoped_budgets else 1
            if not isinstance(budget_scope, int) or not 1 <= budget_scope <= scope_count:
                detail = "action policy returned an invalid budget scope"
                if runtime is not None and runtime_call is not None:
                    runtime.finish_tool_call(
                        runtime_call, raw_result=None, normalized_result=None,
                        succeeded=False, error={"type": "HarnessPolicyError", "message": detail},
                        failure_phase="PRE_TOOL",
                    )
                raise ValueError(detail)
            scope_counts = scoped_tool_counts.setdefault(budget_scope, {})
            tool_limit = self.config.per_tool_limits.get(tool_call.name)
            tool_count = scope_counts.get(tool_call.name, 0)
            if tool_limit is not None and tool_count >= tool_limit:
                detail = f"{tool_call.name} tool-call budget exhausted"
                if scoped_budgets:
                    detail += f" in research round {budget_scope}"
                _append_error(log, detail, C.BUDGET_PER_TOOL)
                if runtime is not None and runtime_call is not None:
                    runtime.finish_tool_call(
                        runtime_call,
                        raw_result=None,
                        normalized_result=None,
                        succeeded=False,
                        error={"type": "HarnessPolicyError", "message": detail},
                        failure_phase="PRE_TOOL",
                    )
                raise budget_error(detail, C.BUDGET_PER_TOOL)
            if (
                tool_call.name == "reader"
                and validated_arguments is not None
                and config.max_total_read_chars is not None
            ):
                batch = getattr(validated_arguments, "reads", None)
                requested = (sum(item.max_chars for item in batch) if batch is not None
                             else getattr(validated_arguments, "max_chars"))
                remaining = config.max_total_read_chars - total_read_chars
                if requested > remaining:
                    detail = "reader cumulative character budget exhausted: request exceeds remaining"
                    _append_error(log, detail, C.BUDGET_READ_CHARS)
                    rejection = {"succeeded": False, "reason": "read_budget_exceeded",
                                 "requested_chars": requested, "remaining_chars": remaining,
                                 "instruction": "Shrink the request to the remaining limit or finish with the evidence already read."}
                    if runtime is not None and runtime_call is not None:
                        runtime.finish_tool_call(
                            runtime_call,
                            raw_result=None,
                            normalized_result=rejection,
                            succeeded=False,
                            error={"type": "HarnessPolicyError", "message": detail},
                            failure_phase="PRE_TOOL",
                        )
                    log.append(ToolCallHarnessEvent(kind="tool_result", tool_call=tool_call,
                        message=self._tool_message(role="tool", tool_call_id=tool_call.id,
                                            content=json.dumps(rejection))))
                    read_budget_rejections += 1
                    if (self.config.allow_one_read_budget_correction
                            and read_budget_rejections == 1 and turn < exploration_turns):
                        continue
                    raise budget_error(detail, C.BUDGET_READ_CHARS)
            log.append(
                ToolCallHarnessEvent(kind="tool_call", tool_call=tool_call,
                    detail=f"research_round:{budget_scope}" if scoped_budgets else None)
            )
            execution_plan = getattr(execution_scope, "search_plan", None)
            cache_key = json.dumps({"arguments": resolved_arguments,
                "search_plan": execution_plan.model_dump(mode="json") if execution_plan is not None else None},
                sort_keys=True, default=str)
            reusable = self.config.reuse_database_results and tool_call.name == "database_search"
            reused = reusable and cache_key in database_results
            try:
                if reused:
                    observation = database_results[cache_key]
                elif validated_arguments is None:
                    observation = await self.registry.execute(
                        tool_call.name,
                        dict(tool_call.arguments),
                        scope=execution_scope,
                    )
                else:
                    observation = await self.registry.execute_validated(
                        tool_call.name, validated_arguments, scope=execution_scope
                    )
                if reusable and not reused and _database_result_complete(observation):
                    database_results[cache_key] = observation
            except BaseException as exc:
                if runtime is not None and runtime_call is not None:
                    runtime.fail_tool_call(runtime_call, exc)
                raise
            try:
                model_context = self.registry.project_model_context(
                    tool_call.name, observation
                )
                if reused:
                    model_context = {**model_context, "reused_result": True,
                        "instruction": "同一任务和检索计划已执行过该来源，复用原结果，未重新请求数据库。读取已有候选或更换来源；无证据则如实收尾。"}
            except BaseException as exc:
                if runtime is not None and runtime_call is not None:
                    runtime.fail_tool_call(
                        runtime_call,
                        exc,
                        raw_result=observation.model_dump(mode="json"),
                        failure_phase="NORMALIZATION",
                    )
                raise
            if runtime is not None and runtime_call is not None:
                runtime.finish_tool_call(
                    runtime_call,
                    raw_result=observation.model_dump(mode="json"),
                    normalized_result=model_context,
                    succeeded=observation.succeeded,
                    error=observation.error,
                    failure_phase=(
                        None if observation.succeeded else "TOOL_EXECUTION"
                    ),
                )
            # Record the completed observation before policy transitions can fail.
            tool_message = self._tool_message(
                role="tool", tool_call_id=tool_call.id,
                content=_serialize_tool_result(model_context),
            )
            log.append(ToolCallHarnessEvent(
                kind="tool_result", message=tool_message,
                tool_call=tool_call, observation=observation,
            ))
            if policy is not None:
                policy.observe(tool_call, observation)
            tool_calls_used += 1
            scope_counts[tool_call.name] = tool_count + 1
            per_tool_counts[tool_call.name] = per_tool_counts.get(tool_call.name, 0) + 1
            if tool_call.name == "database_search" and observation.succeeded:
                required_reader_artifact_ids = _database_artifact_ids(observation)
            elif tool_call.name == "reader" and required_reader_artifact_ids:
                successful_artifacts = {
                    read.get("artifact_id") for read in _reader_results(observation)
                }
                if observation.succeeded and (
                    "read_results" not in observation.payload
                    or successful_artifacts.intersection(required_reader_artifact_ids)
                ):
                    required_reader_artifact_ids.clear()
                else:
                    # 失败时释放「这一个」制品的约束。否则该 id 永远读不出来时，
                    # 模型既不能换一篇读、也不能重新检索（其它工具全被拒），只能
                    # 反复重试同一个坏 id 直到耗尽整轮预算。
                    required_reader_artifact_ids.difference_update(
                        _reader_artifact_ids(resolved_arguments)
                    )
            if tool_call.name == "reader" and observation.succeeded:
                if self.config.reuse_reader_results:
                    reader_state.remember(resolved_arguments, observation, model_context)
                for read in _reader_results(observation):
                    start, end = read.get("char_start"), read.get("char_end")
                    if isinstance(start, int) and isinstance(end, int):
                        total_read_chars += max(0, end - start)
            if config.max_total_read_chars is not None and total_read_chars > config.max_total_read_chars:
                # Preserve the actual observation before finalization, even if
                # a tool violates its requested character bound.
                detail = "reader cumulative character budget exhausted"
                _append_error(log, detail, C.BUDGET_READ_CHARS)
                raise budget_error(detail, C.BUDGET_READ_CHARS)

        detail = ("tool-call exploration limit reached" if tool_calls_used >= config.max_tool_calls
                  else "exploration turn budget exhausted")
        _append_error(log, detail, C.BUDGET_TOOLS if tool_calls_used >= config.max_tool_calls else C.BUDGET_TURNS)
        raise budget_error(
            detail,
            C.BUDGET_TOOLS if tool_calls_used >= config.max_tool_calls else C.BUDGET_TURNS,
        )


class _ReaderState:
    """Invocation-local read facts; never synthesizes evidence or novel text."""

    def __init__(self) -> None:
        self.results: dict[str, dict[str, Any]] = {}
        self.reads: dict[tuple[str, str, str], dict[str, Any]] = {}

    def remember(self, arguments, observation, model_context) -> None:
        rows = _reader_results(observation)
        if not rows or observation.payload.get("read_errors"):
            return
        # Require provenance and text, not a success-shaped stub/result.
        if any(not all(key in row for key in ("artifact_id", "read_id", "namespace",
                   "sha256", "char_start", "char_end", "text", "has_more")) for row in rows):
            return
        self.results[json.dumps(arguments, sort_keys=True)] = model_context
        for row in rows:
            self.reads[(row["namespace"], row["artifact_id"], row["read_id"])] = row

    def replay(self, arguments) -> dict[str, Any] | None:
        requests = arguments.get("reads") or [arguments]
        addresses = []
        for request in requests:
            artifact_id = request.get("artifact_id")
            namespace = request.get("namespace")
            if namespace is None:
                namespaces = {row["namespace"] for row in self.reads.values()
                              if row["artifact_id"] == artifact_id}
                if len(namespaces) != 1:
                    return None
                namespace = next(iter(namespaces))
            addresses.append((namespace, artifact_id))
        previous = self.results.get(json.dumps(arguments, sort_keys=True))
        if previous is not None:
            return {**previous, "reused_result": True, "reader_state": self.project(),
                "instruction": "This exact request was already read. The same verified text and read handles are replayed; no new evidence or read budget was consumed. Read an unread range or another artifact, or finish from existing evidence."}
        known_ends = {}
        for row in self.reads.values():
            # An empty out-of-range request does not establish the true EOF.
            if row["has_more"] is False and row["char_end"] > row["char_start"]:
                known_ends[(row["namespace"], row["artifact_id"])] = row["char_end"]
        if requests and all(address in known_ends and
                request.get("char_start", 0) >= known_ends[address]
                for request, address in zip(requests, addresses)):
            return {"succeeded": True, "read_status": "artifact_eof",
                "reused_result": True, "read_results": [], "reader_state": self.project(),
                "instruction": "All requested offsets are at or beyond a previously verified artifact end. No new read was executed. EOF applies only to these artifacts, not to their whole papers; earlier unread ranges and other artifacts remain readable."}
        return None

    def project(self) -> list[dict[str, Any]]:
        return [{key: row[key] for key in ("namespace", "artifact_id", "read_id",
                    "char_start", "char_end", "has_more")}
                for row in self.reads.values()]


def _reader_artifact_ids(arguments):
    reads = arguments.get("reads")
    rows = reads if isinstance(reads, list) else [arguments]
    return {row["artifact_id"] for row in rows
            if isinstance(row, dict) and isinstance(row.get("artifact_id"), str)}


def _reader_results(observation):
    if "read_results" in observation.payload:
        return observation.payload["read_results"]
    return [observation.payload.get("read_result", {})]


def _database_result_complete(observation):
    if not observation.succeeded:
        return False
    summary = observation.payload.get("execution_summary", {})
    if any(summary.get(key) for key in (
        "partial", "failed", "requires_human", "degraded", "all_failed", "no_execution"
    )):
        return False
    return all(row.get("status") == "succeeded"
               for row in observation.payload.get("search_executions", []))


def _build_tool_definitions(
    registry: Any,
) -> tuple[ToolDefinition, ...]:
    return tuple(
        ToolDefinition(
            name=item["name"],
            description=item["description"],
            parameters=item["arguments_schema"],
        )
        for item in registry.descriptions()
    )


def _build_context(
    system_prompt: str,
    trace: tuple[ToolCallHarnessEvent, ...],
) -> list[ChatMessage]:
    messages = [ChatMessage(role="system", content=system_prompt)]
    messages.extend(
        event.message
        for event in trace
        if event.message is not None
        and event.kind
        in {"initial_user_message", "assistant_response", "tool_result"}
    )
    return messages


def _serialize_tool_result(model_context: dict) -> str:
    return json.dumps(
        model_context,
        ensure_ascii=False,
        sort_keys=True,
    )


def _database_artifact_ids(observation: ResearcherToolObservation) -> set[str]:
    result = observation.payload.get("database_search_result", {})
    return {
        artifact_id
        for item in result.get("results", [])
        for artifact_id in item.get("artifact_ids", [])
        if isinstance(artifact_id, str) and artifact_id
    }


def decision_options(options: ModelCallOptions, choices: dict) -> ModelCallOptions:
    """Current menu only; empty menus restore the caller's draft contract."""
    response_format = options.response_format
    if choices:
        response_format = {"type": "json_schema", "json_schema": {
            "name": "harness_decision", "strict": True, "schema": {
                "type": "object", "properties": {
                    "option_id": {"type": "string", "enum": list(choices)}},
                "required": ["option_id"], "additionalProperties": False}}}
    return replace(options, tools=(), tool_choice="none", response_format=response_format)


def _append_error(log: list[ToolCallHarnessEvent], detail: str, code: C | None = None) -> None:
    log.append(ToolCallHarnessEvent(kind="error", detail=detail, failure_code=code))


def _record_budget_stop(*, scope, code, detail, occurrence_id, budget) -> None:
    from ..schemas.failures import FailureScope, make_failure
    from .correction_trace import record_detection, record_stop

    point = getattr(scope, "novelty_point", None)
    task = getattr(scope, "research_task", None)
    failure = make_failure(
        code,
        scope=FailureScope(
            node="tool_call_harness.budget",
            paper_id=getattr(scope, "subject_paper_id", None),
            run_id=getattr(scope, "run_id", None),
            point_id=getattr(point, "point_id", None),
            task_id=getattr(task, "task_id", None),
            attempt=getattr(task, "attempt", None),
        ),
        message="The Harness reached an explicit execution budget and stopped local correction.",
        occurrence_id=occurrence_id,
    )
    correction = record_detection(
        failure,
        owner_node="tool_call_harness.budget",
        owner_function="ToolCallHarness._run",
        action_input={"detail": detail},
        budget=budget,
    )
    record_stop(
        correction,
        reason=detail,
        remaining_issues=[code.value],
        result={"executed": False, "budget_denied": True},
    )


RUNTIME_STATE_PREFIX = "MACHINE_RUNTIME_STATE\n"


def _with_runtime_state(messages, state):
    # The snapshot is rebuilt outside the trace: exactly one state message per
    # model request. Original task, assistant and tool messages are untouched.
    return [messages[0], ChatMessage(role="system", content=RUNTIME_STATE_PREFIX +
        json.dumps(state, ensure_ascii=False, sort_keys=True)), *messages[1:]]


def _project_runtime_state(config, trace, *, scope, invocation_id, phase,
                           stop_reason, remaining_turns, tool_calls_used,
                           per_tool_counts, total_read_chars,
                           required_reader_artifact_ids, deadline):
    """Project deterministic facts only; no text summarization or evidence judgement."""
    readers, providers, checkpoints = {}, {}, {}
    for event in trace:
        observation = event.observation
        if observation is None:
            continue  # Reader replay has no new observation or read expenditure.
        payload = observation.payload
        tool_id = event.tool_call.id if event.tool_call else None
        if observation.tool_name == "reader" and observation.succeeded:
            for read in _reader_results(observation):
                if not all(isinstance(read.get(key), str) and read[key]
                           for key in ("namespace", "artifact_id", "sha256", "read_id")):
                    continue
                start, end = read.get("char_start"), read.get("char_end")
                if type(start) is not int or type(end) is not int or not 0 <= start <= end:
                    continue
                address = (read["namespace"], read["artifact_id"], read["sha256"])
                entry = readers.setdefault(address, {"namespace": address[0], "artifact_id": address[1],
                    "sha256": address[2], "ranges": {}, "ends": set(), "empty_read_count": 0})
                if end > start:
                    entry["ranges"].setdefault((start, end), {"char_start": start,
                        "char_end": end, "read_id": read["read_id"]})
                    if read.get("has_more") is False:
                        entry["ends"].add(end)
                else:
                    entry["empty_read_count"] += 1
                    entry["latest_empty_offset"] = start
        elif observation.tool_name == "database_search":
            database_result = payload.get("database_search_result")
            database_result = database_result if isinstance(database_result, dict) else {}
            source = (database_result.get("source_id")
                      or observation.arguments.get("source_id"))
            if not isinstance(source, str) or not source:
                source = "unknown_source"
            entry = providers.setdefault(source, {"source_id": source, "observed_tool_results": 0,
                "replayed_results": 0, "failed_tool_results": 0, "partial_tool_results": 0,
                "successful_tool_results": 0, "first_failure": None, "last_failure": None})
            context_text = event.message.content if event.message and event.message.content else "{}"
            model_context = json.loads(context_text.removeprefix("Harness observation:\n"))
            reused = model_context.get("reused_result") is True
            entry["observed_tool_results"] += 1
            if reused:
                entry["replayed_results"] += 1
            summary = payload.get("execution_summary")
            summary = summary if isinstance(summary, dict) else {}
            failed = not observation.succeeded or bool(summary.get("all_failed"))
            partial = not failed and any(summary.get(key) for key in (
                "failed", "partial", "degraded", "requires_human", "retrieval_incomplete_budget"))
            no_execution = bool(summary.get("no_execution"))
            status = "failed" if failed else "partial" if partial else "not_executed" if no_execution else "succeeded"
            if not reused:
                entry[{"failed": "failed_tool_results", "partial": "partial_tool_results"}.get(
                    status, "successful_tool_results")] += int(status != "not_executed")
            result_rows = database_result.get("results")
            known_empty = status == "succeeded" and bool(summary.get("succeeded")) and result_rows == []
            entry["latest"] = {"tool_call_id": tool_id, "status": status,
                "reused_result": reused, "execution_summary": dict(summary),
                "successful_call_returned_zero_candidates": known_empty}
            if (failed or partial) and not reused:
                error = observation.error or observation.summary
                failure = {"tool_call_id": tool_id, "status": status,
                    "origin": "provider_execution" if summary.get("provider_failed") else "tool_or_provider_unknown",
                    "error": error[:512], "error_truncated": len(error) > 512,
                    "execution_summary": dict(summary)}
                if entry["first_failure"] is None:
                    entry["first_failure"] = failure
                entry["last_failure"] = failure
        elif observation.tool_name == "submit_evidence" and observation.succeeded:
            checkpoint_id = payload.get("checkpoint_id")
            if (payload.get("durable") is not True or payload.get("validation_stage") != "builder_provenance_only"
                    or not isinstance(checkpoint_id, str) or not checkpoint_id):
                continue
            entry = checkpoints.setdefault(checkpoint_id, {"checkpoint_id": checkpoint_id,
                "acknowledged_card_ids": set(), "validation_stage": "builder_provenance_only",
                "downstream_validator_status": "not_determined_by_harness",
                "reviewer_status": "not_determined_by_harness"})
            entry["acknowledged_card_ids"].update(value for value in payload.get("accepted_card_ids", [])
                                                  if isinstance(value, str) and value)
            entry["last_acknowledged_total_cards"] = payload.get("total_checkpoint_cards")
            entry["last_tool_call_id"] = tool_id
    read_rows = []
    for address, entry in sorted(readers.items()):
        ends = entry.pop("ends")
        ranges = sorted(entry.pop("ranges").values(), key=lambda item: (item["char_start"], item["char_end"]))
        eof = next(iter(ends)) if len(ends) == 1 else None
        covered_to = 0
        for row in ranges:
            if row["char_start"] <= covered_to:
                covered_to = max(covered_to, row["char_end"])
        read_rows.append({**entry, "read_ranges": ranges, "verified_artifact_end": eof,
            "end_status": "verified" if len(ends) == 1 else "conflicting" if ends else "unknown",
            "whole_artifact_read": eof is not None and covered_to >= eof,
            "whole_paper_read": "not_inferred_from_artifact"})
    checkpoint_rows = [{**entry, "acknowledged_card_ids": sorted(entry["acknowledged_card_ids"])}
                       for _, entry in sorted(checkpoints.items())]
    scope_value = scope.model_dump(mode="json") if hasattr(scope, "model_dump") else scope if isinstance(scope, dict) else {}
    point = scope_value.get("novelty_point", {})
    task = scope_value.get("research_task", {})
    return {"schema_version": "1.0", "invocation_id": invocation_id,
        "scope": {"paper_id": scope_value.get("subject_paper_id"), "run_id": scope_value.get("run_id"),
            "point_id": point.get("point_id"), "task_id": task.get("task_id"),
            "attempt": scope_value.get("attempt") if scope_value.get("attempt") is not None else task.get("attempt")},
        "phase": phase, "stop_reason": stop_reason,
        "budget": {"remaining_exploration_calls_including_current": max(0, remaining_turns),
            "exploration_call_limit": config.max_turns - int(config.reserve_final_turn),
            "finalization_enabled": config.finalize_on_budget,
            "finalization_inside_max_turns": config.reserve_final_turn,
            "current_call_tool_execution_allowed": phase == "exploration",
            "charged_tool_calls": tool_calls_used, "remaining_tool_calls": max(0, config.max_tool_calls - tool_calls_used),
            "charged_read_chars": total_read_chars,
            "remaining_read_chars": None if config.max_total_read_chars is None else max(0, config.max_total_read_chars - total_read_chars),
            "remaining_per_tool": {name: max(0, limit - per_tool_counts.get(name, 0))
                                   for name, limit in sorted(config.per_tool_limits.items())},
            "remaining_exploration_seconds": max(0, round(deadline - time.monotonic(), 1))
                if isinstance(deadline, (int, float)) else None,
            "global_runtime_budget": "not_owned_by_this_harness"},
        "required_reader_artifact_ids": sorted(required_reader_artifact_ids),
        "reader_artifacts": read_rows, "providers": [row for _, row in sorted(providers.items())],
        "evidence_checkpoints": checkpoint_rows,
        "semantic_evidence_sufficiency": "not_determined_by_harness",
        "provider_failure_means_no_matching_literature": False,
        "history_policy": "complete_original_task_assistant_tool_messages_preserved"}
