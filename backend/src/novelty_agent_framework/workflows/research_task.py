"""Native tool-calling workflow for one formal ResearchTask."""

from __future__ import annotations

from ..schemas.failures import CodedFailure, failure_family
from ..core.failure_classification import output_failure_code, model_failure_code

import json
import re
from pathlib import Path
from dataclasses import dataclass, field, replace

from backend.env import ChatMessage, ModelCallOptions, ModelClient, PromptLibrary
from pydantic import ValidationError

from ..core.format_repair import repair_json
from ..core import local_correction as local
from ..schemas.failures import RecoveryAction
from ..core import ToolCallHarness, ToolCallHarnessConfig, ToolCallHarnessError
from ..schemas import (
    ReferenceReadResult,
    SearchPlan,
    ResearchBundle,
    ResearchFinishDraft,
    TaskResearchRequest,
)
from ..schemas import TaskResearchResult, TaskResearchStatus
from ..schemas.references import SearchExecution
from ..tools import EvidenceCardBuilder, ResearcherToolRegistry
from ..tools.evidence_checkpoint import EvidenceCheckpointSession, EvidenceCheckpointRegistry
from .candidate_audit import build_candidate_audit
from .research_reading_policy import ResearchReadingPolicy
from ..tools.recovery_registry import RecoveryToolRegistry
from ..schemas.failures import FailureEvent, FailureScope, FailureCode, make_failure


def _extract_finish_json(content: str | None) -> str:
    """从模型收尾文本中稳健提取 ResearchFinishDraft JSON 片段。

    DeepSeek-V4-Flash 等模型常把收尾 JSON 包在 ```json ... ``` 代码块里或
    夹带叙述文字；pydantic 的 model_validate_json 要求纯 JSON，这里先剥离
    Markdown 围栏，再截取首个 '{' 到末尾 '}' 之间的内容。
    """

    text = (content or "").strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        text = text[start : end + 1]
    return text


@dataclass(frozen=True)
class TaskResearcherConfig:
    """Formal harness budgets plus temporarily retained legacy settings."""

    max_steps: int = 12
    max_correction_model_calls: int = 3
    max_tool_calls: int = 10
    max_chars_per_read: int = 8_000
    max_total_read_chars: int = 48_000
    per_tool_limits: dict[str, int] = field(
        default_factory=lambda: {
            "database_search": 2,
            "web_search": 3,
            "browser": 3,
            "reader": 8,
        }
    )
    model_options: ModelCallOptions = field(
        default_factory=lambda: ModelCallOptions(temperature=0.0, tool_choice="auto")
    )
    prompt_name: str = "research/native_tool_loop"
    reuse_reader_results: bool = False
    enable_evidence_checkpoint: bool = False
    runtime_state_projection: bool = False
    guided_reading: bool = False
    text_choices: bool = False

    def __post_init__(self) -> None:
        if self.max_correction_model_calls < 0:
            raise ValueError("correction model budget must be nonnegative")
        if self.text_choices and not self.guided_reading:
            raise ValueError("text_choices requires guided_reading=true")
        if self.guided_reading and self.enable_evidence_checkpoint:
            raise ValueError("guided_reading currently requires enable_evidence_checkpoint=false")
        if min(self.max_steps, self.max_tool_calls, self.max_chars_per_read,
               self.max_total_read_chars) < 1:
            raise ValueError("researcher budgets must be positive")


class TaskResearcherWorkflow:
    """Drive a model through native tools, then compile trusted observations."""

    def __init__(
        self,
        model_client: ModelClient,
        tool_registry: ResearcherToolRegistry,
        evidence_builder: EvidenceCardBuilder,
        *,
        prompts: PromptLibrary | None = None,
        config: TaskResearcherConfig | None = None,
    ) -> None:
        self.model_client = model_client
        self.tools = tool_registry
        self.evidence_builder = evidence_builder
        self.prompts = prompts
        self.config = config or TaskResearcherConfig()
        self.harness = ToolCallHarness(
            model_client,
            tool_registry,
            action_policy_factory=(lambda registry, scope: ResearchReadingPolicy(
                registry, scope, max_chars=min(self.config.max_chars_per_read, 16000)))
                if self.config.guided_reading else None,
            config=ToolCallHarnessConfig(
                round_budgets=self.config.guided_reading,
                text_choices=self.config.text_choices,
                finalize_on_budget=True,
                reuse_database_results=True,
                reuse_reader_results=self.config.reuse_reader_results,
                runtime_state_projection=self.config.runtime_state_projection,
                max_turns=self.config.max_steps,
                max_tool_calls=self.config.max_tool_calls,
                per_tool_limits=dict(self.config.per_tool_limits),
                max_total_read_chars=self.config.max_total_read_chars,
            ),
        )

    async def ainvoke(self, request: TaskResearchRequest) -> TaskResearchResult:
        request = TaskResearchRequest.model_validate(request)
        scopes = (ResearchReadingPolicy(self.tools, request).budget_scope_count
                  if self.config.guided_reading else 1)
        session = local.LocalSession(
            model_limit=self.config.max_steps * scopes + 1 + self.config.max_correction_model_calls,
            correction_model_limit=self.config.max_correction_model_calls,
            per_tool_limits={name:limit*scopes for name,limit in self.config.per_tool_limits.items()},
            tool_limit=self.config.max_tool_calls * scopes,
            read_limit=self.config.max_total_read_chars * scopes)
        session.attach(request)
        with session.bind():
            session.close_pending()
            result = await self._invoke(request)
            session.close_pending()
        return result.model_copy(update={"correction_budget": session.snapshot(),
            "correction_records": list(session.records.values())})

    async def _invoke(self, request: TaskResearchRequest) -> TaskResearchResult:
        request = TaskResearchRequest.model_validate(request)
        checkpoint = (EvidenceCheckpointSession(self.evidence_builder, request)
                      if self.config.enable_evidence_checkpoint else None)
        registry = EvidenceCheckpointRegistry(self.tools, checkpoint) if checkpoint else self.tools
        if request.recovery is not None:
            registry = RecoveryToolRegistry(registry, request)
        harness = (ToolCallHarness(self.model_client, registry, config=self.harness.config,
                                   action_policy_factory=self.harness.action_policy_factory)
                   if checkpoint or request.recovery is not None else self.harness)
        result, trace = await self._research(request, harness=harness)
        if checkpoint is not None:
            result = checkpoint.merge_into(result)
        result, trace = await self._retry_failed_requests(request, result, trace, registry)
        failures = [*result.execution_failures, *(failure for bundle in result.research_bundles for failure in bundle.execution_failures),
                    *(e.failure for e in _search_audit(trace) if e.failure is not None),
                    *_observed_tool_failures(trace, request)]
        return result.model_copy(update={"harness_diagnostics": _harness_diagnostics(trace, request),
                                        "execution_failures": list({f.event_id:f for f in failures}.values()),
                                        "search_executions": _search_audit(trace), "candidate_audit": build_candidate_audit(
            trace, result.read_results, result.evidence, result.evidence_cards,
            interrupted=result.status == TaskResearchStatus.PARTIAL,
        )})

    async def _retry_failed_requests(self, request, result, trace, registry):
        from ..core.tool_call_harness import ToolCallHarnessEvent
        from backend.env import ModelToolCall
        if request.recovery is not None:
            # A legacy frozen recovery task already spends its declared attempt;
            # it cannot silently acquire a second task-end recovery allowance.
            return result, trace
        retryable = {FailureCode.PROVIDER_NETWORK, FailureCode.PROVIDER_RATE_LIMIT, FailureCode.PROVIDER_SERVICE}
        log = list(trace)
        session = local.current_session.get()
        for event in trace:
            obs = event.observation
            if event.kind != "tool_result" or obs is None or obs.tool_name != "database_search":
                continue
            failed = [e for e in _search_audit([event]) if e.failure is not None
                      and e.status.value == "failed" and failure_family(e.failure.code) in retryable]
            if not failed or obs.arguments.get("full_text_source_record_ids"):
                continue
            frozen = event.execution_scope or request
            source = failed[0].source_id
            successful = {(e.source_id, e.query) for e in _search_audit(trace) if e.status.value == "succeeded"}
            queries = {e.query for e in failed if e.source_id == source
                       and (source, e.query) not in successful
                       and session.once("retry:" + source + ":" + e.query)}
            if not queries:
                continue
            record = local.detect(request, failed[0].failure.code, "TaskResearcherWorkflow.ainvoke",
                failure=failed[0].failure, message="Retry original failed queries",
                inputs={"source_id": source, "queries": sorted(queries),
                        "search_plan": frozen.search_plan.model_dump(mode="json")},
                targets={"source_ids": [source]})
            if any(failure_family(f.code) == FailureCode.BUDGET_EXHAUSTED for f in result.execution_failures):
                local.stop(record, "Original task stopped at a budget boundary")
                continue
            try:
                session.check_tool("database_search")
            except CodedFailure as exc:
                local.stop(record, str(exc))
                continue
            record = local.begin(record, RecoveryAction.RETRY_REQUEST)
            token = local.retry_queries.set(queries)
            try:
                retried = await local.audited_call("database_search", {"source_id": source},
                    lambda: registry.execute("database_search", {"source_id": source}, scope=frozen))
            finally:
                local.retry_queries.reset(token)
            new_event = ToolCallHarnessEvent(kind="tool_result", observation=retried,
                tool_call=ModelToolCall("end-retry", "database_search", {"source_id": source}), execution_scope=frozen)
            log.append(new_event)
            executions = _search_audit([new_event])
            passed = bool(executions) and all(e.status.value == "succeeded" for e in executions)
            local.finish(record, passed, "SearchExecution.status",
                result={"executions": [e.model_dump(mode="json") for e in executions]},
                reason="Original query retry failed or was blocked")
            if not passed:
                continue
            # Acquired text must pass Reader before any new evidence is compiled.
            artifacts = retried.payload.get("artifacts", [])
            for artifact in artifacts:
                if "reader" not in registry.names:
                    break
                aid = artifact.get("artifact_id")
                if not aid or any(r.artifact_id == aid for r in result.read_results):
                    continue
                if session.tool_used >= session.tool_limit or session.read_used >= session.read_limit:
                    break
                arguments = {"artifact_id": aid,
                    "max_chars": min(self.config.max_chars_per_read, session.read_limit-session.read_used)}
                read_obs = await local.audited_call("reader", arguments,
                    lambda: registry.execute("reader", arguments, scope=request))
                log.append(ToolCallHarnessEvent(kind="tool_result", observation=read_obs))
        reads, warnings = _trusted_reads(log)
        bundles, bundle_warnings = _trusted_bundles(log)
        new_reads = [r for r in reads if r.read_id not in {old.read_id for old in result.read_results}]
        if new_reads:
            try:
                local.charge_correction_model()
                response = await self.model_client.acomplete([
                    ChatMessage(role="system", content="Build evidence drafts only from the supplied reference reads. Copy quotes verbatim; never quote the target claim. Return ResearchFinishDraft JSON, or cards=[] with no_evidence_reason. No tools."),
                    ChatMessage(role="user", content=json.dumps({"reader_results": [r.model_dump(mode="json") for r in new_reads],
                        "novelty_point": _project_novelty_point(request.novelty_point),
                        "schema": ResearchFinishDraft.model_json_schema()}, ensure_ascii=False))],
                    options=replace(self.config.model_options, tools=(), tool_choice="none"))
                if response.tool_calls:
                    raise CodedFailure(FailureCode.CORRECTION_NATIVE_CALL, "Retry material compilation attempted a tool call")
                draft, _ = _parse_finish(response.content)
                built = self.evidence_builder.build(draft, scope=request, read_results=reads)
                if built.rejections:
                    built = await self._correct_rejected_cards(draft, built, reads, request)
                existing = {c.card_id for c in result.evidence_cards}
                cards = [c for c in built.evidence_cards if c.card_id not in existing]
                ids = {eid for c in cards for eid in c.evidence_ids}
                result = result.model_copy(update={"evidence_cards": [*result.evidence_cards, *cards],
                    "evidence": [*result.evidence, *(e for e in built.evidence if e.evidence_id in ids)],
                    "card_build_rejections": [*result.card_build_rejections, *built.rejections],
                    "card_correction_rejections": [*result.card_correction_rejections, *built.correction_rejections],
                    "card_correction_failure": built.correction_failure or result.card_correction_failure,
                    "steps_used": result.steps_used + 1})
            except Exception as exc:
                result = result.model_copy(update={"execution_failures": [*result.execution_failures,
                    _execution_failure(request, exc, node="researcher.retry.compile")]})
        return result.model_copy(update={"read_results": list({r.read_id:r for r in [*result.read_results, *reads]}.values()),
            "research_bundles": [*result.research_bundles, *bundles[len(result.research_bundles):]],
            "warnings": [*result.warnings, *warnings, *bundle_warnings]}), tuple(log)

    async def correct_validation(self, request, result, rejected, *, validator, tasks, points, reference_store):
        """Repair rejected cards in their original task, retaining the task's budget."""
        import hashlib
        if not result.correction_budget:
            # Older checkpoints carry no trustworthy remaining allowance.
            records = list(result.correction_records)
            handled = list(result.validator_corrected_card_ids)
            for card_id, reason in rejected:
                if card_id in handled:
                    continue
                record = local.detect(request, FailureCode.MODEL_OUTPUT_REFERENCE,
                    "TaskResearcherWorkflow.correct_validation", message=reason,
                    targets={"card_ids": [card_id]})
                records.append(local.stop(record, "Original task has no recoverable budget snapshot"))
                handled.append(card_id)
            return result.model_copy(update={"correction_records": records,
                "validator_corrected_card_ids": handled})
        session = local.LocalSession(**result.correction_budget)
        session.records = {r.correction_id: r for r in result.correction_records}
        session.attach(request)
        cards = list(result.evidence_cards)
        evidence = list(result.evidence)
        handled = list(result.validator_corrected_card_ids)
        failures = list(result.execution_failures)
        with session.bind():
            for card_id, reason in rejected:
                original = next((c for c in cards if c.card_id == card_id), None)
                if original is None or not session.once("validator:" + card_id):
                    continue
                handled.append(card_id)
                linked = [e for e in evidence if e.evidence_id in original.evidence_ids]
                record = local.detect(request, FailureCode.MODEL_OUTPUT_REFERENCE,
                    "TaskResearcherWorkflow.correct_validation", message=reason,
                    inputs={"card": original.model_dump(mode="json"), "rejection": reason},
                    targets={"card_ids": [card_id], "evidence_ids": original.evidence_ids,
                             "material_ids": list({e.artifact_id for e in linked})},
                    versions=local.read_versions([r for r in result.read_results
                        if r.artifact_id in {e.artifact_id for e in linked}]))
                if not linked or not result.read_results:
                    local.stop(record, "No original evidence or trusted reads available for repair")
                    continue
                try:
                    local.charge_correction_model()
                    record = local.begin(record, RecoveryAction.REPAIR_OUTPUT)
                    response = await self.model_client.acomplete([
                        ChatMessage(role="system", content="Correct only this rejected evidence card. Use verbatim reference reader text, never the target input. Return ResearchFinishDraft JSON with at most one card, or cards=[] and no_evidence_reason. Do not invent IDs. No tools."),
                        ChatMessage(role="user", content=json.dumps({"card": original.model_dump(mode="json"),
                            "validator_rejection": reason,
                            "reader_results": [r.model_dump(mode="json") for r in result.read_results
                                               if r.work_id in {e.work_id for e in linked}],
                            "schema": ResearchFinishDraft.model_json_schema()}, ensure_ascii=False))],
                        options=replace(self.config.model_options, tools=(), tool_choice="none"))
                    if response.tool_calls:
                        raise CodedFailure(FailureCode.CORRECTION_NATIVE_CALL, "Validator correction attempted tools")
                    draft, _ = _parse_finish(response.content)
                    if len(draft.cards) > 1:
                        raise CodedFailure(FailureCode.CORRECTION_CARD_COUNT, "Only the rejected card can be corrected")
                    built = self.evidence_builder.build(draft, scope=request, read_results=result.read_results)
                    checked = validator.validate(built.evidence_cards, tasks=tasks, evidence=built.evidence,
                        novelty_points=points, paper_id=request.subject_paper_id, reference_store=reference_store)
                    import inspect
                    if inspect.isawaitable(checked):
                        checked = await checked
                    valid = bool(built.evidence_cards) and not built.rejections and not checked.rejected
                    allowed_works = {e.work_id for e in linked}
                    valid = valid and all(e.work_id in allowed_works for e in built.evidence)
                    # Never replace another valid card as a side effect of correction.
                    other_ids = {c.card_id for c in cards if c.card_id != card_id}
                    valid = valid and not any(c.card_id in other_ids for c in built.evidence_cards)
                    local.finish(record, valid, "DefaultEvidenceValidator.validate",
                        result={"before_sha256": hashlib.sha256(original.model_dump_json().encode()).hexdigest(),
                            "after_cards": [c.model_dump(mode="json") for c in checked.accepted],
                            "after_sha256": [hashlib.sha256(c.model_dump_json().encode()).hexdigest() for c in checked.accepted],
                            "rejections": list(checked.rejected)},
                        reason="Corrected card still fails source, quote, location, or task validation",
                        versions=local.read_versions([r for r in result.read_results
                            if r.artifact_id in {e.artifact_id for e in built.evidence}]))
                    if valid:
                        cards = [c for c in cards if c.card_id != card_id] + list(checked.accepted)
                        needed = {eid for c in cards for eid in c.evidence_ids}
                        by_id = {e.evidence_id:e for e in evidence}
                        by_id.update({e.evidence_id:e for e in built.evidence})
                        evidence = [e for eid,e in by_id.items() if eid in needed]
                except Exception as exc:
                    local.stop(record, "Validator correction stopped: " + _safe_error(exc))
                    failures.append(_execution_failure(request, exc, node="validator.correction"))
        return result.model_copy(update={"evidence_cards": cards, "evidence": evidence,
            "validator_corrected_card_ids": handled, "execution_failures": failures,
            "correction_budget": session.snapshot(),
            "correction_records": list(session.records.values()),
            "steps_used": result.steps_used + session.model_used-result.correction_budget["model_used"]})

    def recover_checkpoint(self, request: TaskResearchRequest, *, invocation_id: str) -> TaskResearchResult:
        """Explicit recovery only; no task is restarted and no verdict is promoted."""
        return EvidenceCheckpointSession.recover(self.evidence_builder, request,
                                                invocation_id=invocation_id)

    async def _research(self, request: TaskResearchRequest, *, harness=None):
        request = TaskResearchRequest.model_validate(request)
        system_prompt, user_message = self._render_prompt(request, tool_names=(harness or self.harness).registry.names)
        try:
            harness_result = await (harness or self.harness).run(
                system_prompt=system_prompt,
                initial_user_message=user_message,
                scope=request,
                options=self.config.model_options,
            )
        except ToolCallHarnessError as exc:
            reads, read_warnings = _trusted_reads(exc.trace)
            bundles, bundle_warnings = _trusted_bundles(exc.trace)
            return _partial(
                request,
                reads=reads,
                bundles=bundles,
                warnings=[
                    f"native tool harness failed: {_safe_error(exc)}",
                    *read_warnings,
                    *bundle_warnings,
                ],
                steps=_turns_in_trace(exc.trace),
                failures=[_execution_failure(request, exc)],
            ), exc.trace
        except Exception as exc:
            return _partial(
                request,
                warnings=[f"native tool harness failed: {_safe_error(exc)}"],
                failures=[_execution_failure(request, exc)],
            ), ()

        reads, read_warnings = _trusted_reads(harness_result.trace)
        bundles, bundle_warnings = _trusted_bundles(harness_result.trace)
        format_turns = 0
        format_warnings = []
        try:
            try:
                draft, format_warnings = _parse_finish(harness_result.final_content)
            except (ValidationError, ValueError):
                format_turns = 1
                local.charge_correction_model()
                repaired = await repair_json(self.model_client, harness_result.final_content,
                                             ResearchFinishDraft.model_json_schema(), self.config.model_options)
                draft, format_warnings = _parse_finish(repaired)
                format_warnings.append("single format repair recovered ResearchFinishDraft")
        except Exception as exc:
            return _partial(
                request, reads=reads, bundles=bundles,
                warnings=[*read_warnings, *bundle_warnings,
                          f"invalid ResearchFinishDraft after bounded recovery: {_safe_error(exc)}"],
                steps=harness_result.turns_used + format_turns,
                failures=[_execution_failure(request, exc, default=FailureCode.MODEL_OUTPUT_SCHEMA)],
            ), harness_result.trace

        try:
            built = self.evidence_builder.build(
                draft, scope=request, read_results=reads
            )
        except Exception as exc:
            failure = _execution_failure(request, exc, default=FailureCode.MODEL_OUTPUT_REFERENCE,
                                         node="builder.compile")
            record = local.detect(request, failure.code, "EvidenceCardBuilder.build",
                failure=failure, message="Whole builder invocation failed",
                targets={"material_ids": [r.artifact_id for r in reads]})
            rebuilt = False
            if isinstance(exc, OSError):
                try:
                    local.charge("tool")
                    record = local.begin(record, RecoveryAction.REPAIR_OUTPUT)
                    built = self.evidence_builder.build(draft, scope=request, read_results=reads)
                    local.finish(record, not built.rejections, "EvidenceCardBuilder.build",
                        result={"card_ids": [c.card_id for c in built.evidence_cards]},
                        reason="Builder retry still rejected drafts")
                    rebuilt = True
                except Exception as retry_exc:
                    local.stop(record, "Whole builder retry failed: " + _safe_error(retry_exc))
            else:
                local.stop(record, "Whole builder failure is not a transient I/O error")
            if not rebuilt:
                return _partial(request, reads=reads, bundles=bundles,
                    warnings=[*read_warnings, *bundle_warnings, f"evidence builder failed: {_safe_error(exc)}"],
                    steps=harness_result.turns_used + format_turns, failures=[failure]), harness_result.trace

        initial_rejections = list(built.rejections)
        correction_turns = 0
        repairable = [item for item in built.rejections
                      if item.code != FailureCode.BUILDER_WEB_SUPPLEMENT]
        if repairable:
            correction_turns = 1
            built = await self._correct_rejected_cards(
                draft, built.model_copy(update={"rejections": repairable}), reads, request,
            )
        stop_warnings = ([f"research stopped; finalization completed: {harness_result.stop_reason}"]
                         if harness_result.stop_reason else [])
        return TaskResearchResult(
            task_id=request.research_task.task_id,
            novelty_point_id=request.novelty_point.point_id,
            status=(TaskResearchStatus.PARTIAL if harness_result.stop_reason else TaskResearchStatus.COMPLETED),
            read_results=reads,
            research_bundles=bundles,
            evidence=built.evidence,
            evidence_cards=built.evidence_cards,
            card_build_rejections=initial_rejections,
            card_correction_rejections=built.correction_rejections,
            card_correction_failure=built.correction_failure,
            warnings=[*stop_warnings, *read_warnings, *bundle_warnings, *format_warnings, *built.warnings],
            steps_used=harness_result.turns_used + format_turns + correction_turns,
            execution_failures=([make_failure(_budget_code(harness_result.trace), scope=_failure_scope(request).model_copy(update={"node": "harness.finalize"}),
                message="Research exploration budget exhausted; bounded finalization retained available evidence.",
                occurrence_id="exploration-finalization")] if harness_result.stop_reason else []),
        ), harness_result.trace

    async def _correct_rejected_cards(self, draft, built, reads, request):
        payload = {
            "novelty_point": _project_novelty_point(request.novelty_point),
            "rejected_cards": [
                {"card": draft.cards[item.card_index].model_dump(mode="json"), "error": item.reason, "error_code": item.code.value}
                for item in built.rejections
            ],
            "reader_results": [read.model_dump(mode="json") for read in reads],
            "finish_schema": ResearchFinishDraft.model_json_schema(),
        }
        records = [local.detect(request, item.code, "TaskResearcherWorkflow._correct_rejected_cards",
            message=item.reason, inputs={"card_index": item.card_index},
            targets={"material_ids": [read.artifact_id for read in reads]},
            versions=local.read_versions(reads)) for item in built.rejections]
        payload["source_boundary"] = {
            "target_input_is_not_reference": True,
            "target_identity": request.target_identity.model_dump(mode="json") if request.target_identity else None,
            "rule": "Reject target-paper claims as quotes. Use only verbatim reader_results text from a reference work; otherwise return no evidence.",
        }
        try:
            local.charge_correction_model()
            records = [local.begin(record, RecoveryAction.REPAIR_OUTPUT) for record in records]
            response = await self.model_client.acomplete([
                ChatMessage(role="system", content=(
                    "Correct only the rejected evidence drafts. No tools are available. "
                    "Copy quotes verbatim from reader_results[].text, preserving LaTeX and punctuation. Group each card by source work. "
                    "Do not invent provenance IDs. Return ResearchFinishDraft JSON, or "
                    "cards=[] and a no_evidence_reason if the drafts cannot be grounded."
                )),
                ChatMessage(role="user", content=json.dumps(payload, ensure_ascii=False)),
            ], options=replace(self.config.model_options, tools=(), tool_choice="none"))
            if response.tool_calls:
                raise CodedFailure(FailureCode.CORRECTION_NATIVE_CALL, "quote correction attempted a tool call")
            corrected = ResearchFinishDraft.model_validate_json(_extract_finish_json(response.content))
            if len(corrected.cards) > len({(r.namespace, r.work_id) for r in reads}):
                raise CodedFailure(FailureCode.CORRECTION_CARD_COUNT, "correction returned more cards than observed source works")
            repaired = self.evidence_builder.build(corrected, scope=request, read_results=reads)
            for record in records:
                local.finish(record, len(repaired.evidence_cards) >= len(built.rejections) and not repaired.rejections,
                    "EvidenceCardBuilder.build", result={"card_ids": [c.card_id for c in repaired.evidence_cards],
                    "evidence_ids": [e.evidence_id for e in repaired.evidence],
                    "rejections": [r.model_dump(mode="json") for r in repaired.rejections]},
                    reason="Corrected drafts still rejected or no grounded reference quote",
                    versions=local.read_versions([read for read in reads
                        if read.artifact_id in {e.artifact_id for e in repaired.evidence}]))
            existing = {card.card_id for card in built.evidence_cards}
            new_cards = [card for card in repaired.evidence_cards if card.card_id not in existing]
            needed_ids = {eid for card in new_cards for eid in card.evidence_ids}
            evidence_by_id = {item.evidence_id: item for item in built.evidence}
            evidence_by_id.update({item.evidence_id: item for item in repaired.evidence if item.evidence_id in needed_ids})
            return built.model_copy(update={
                "evidence": list(evidence_by_id.values()),
                "evidence_cards": [*built.evidence_cards, *new_cards],
                "correction_rejections": [item.model_copy(update={"node": "builder.correction"})
                                          for item in repaired.rejections],
                "warnings": [*built.warnings, *repaired.warnings,
                             f"single quote correction recovered {len(new_cards)} card(s)"],
            })
        except Exception as exc:
            for record in records:
                local.stop(record, "Builder correction failed: " + _safe_error(exc))
            return built.model_copy(update={"warnings": [
                *built.warnings, f"single quote correction failed: {_safe_error(exc)}",
            ], "correction_failure": _execution_failure(request, exc, node="builder.correction")})

    def _render_prompt(self, request: TaskResearchRequest, *, tool_names=None) -> tuple[str, str]:
        variables = {
            "novelty_point_json": json.dumps(
                _project_novelty_point(request.novelty_point), ensure_ascii=False
            ),
            "research_task_json": json.dumps(
                request.research_task.model_dump(mode="json"), ensure_ascii=False
            ),
            "search_plan_json": json.dumps(
                _project_search_plan(request.search_plan), ensure_ascii=False
            ),
            "finish_schema_json": json.dumps(
                ResearchFinishDraft.model_json_schema(), ensure_ascii=False
            ),
        }
        if self.config.text_choices:
            # This mode has a separate configurable prompt, not native-tool skills
            # plus an appended contradictory instruction.
            library = self.prompts or PromptLibrary(Path(__file__).resolve().parents[1] / "prompts")
            name = ("research/choice_only" if self.config.prompt_name == "research/native_tool_loop"
                    else self.config.prompt_name)
            rendered = library.render(name, **variables)
            return rendered.system, rendered.user
        recovery_note = ("\nRuntime-owned recovery directive (tool permissions are enforced): " +
                         request.recovery.model_dump_json() if request.recovery else "")
        if self.prompts is not None:
            rendered = self.prompts.render(self.config.prompt_name, **variables)
            return rendered.system + self._research_skills(), rendered.user + "\n\n" + self._capability_note(tool_names) + recovery_note
        return (
            "Use only registered tools. Finish with strict ResearchFinishDraft JSON. "
            "Never invent provenance handles." + self._research_skills(),
            "\n".join(f"{key}: {value}" for key, value in variables.items()) + "\n" + self._capability_note(tool_names) + recovery_note,
        )

    @staticmethod
    def _research_skills() -> str:
        root = Path(__file__).resolve().parents[1] / "skills"
        return "\n\n" + "\n\n".join(
            (root / name / "SKILL.md").read_text(encoding="utf-8").split("---", 2)[-1].strip()
            for name in ("database_research", "web_supplement")
        )

    def _capability_note(self, tool_names=None):
        if self.config.guided_reading:
            return (
                "Available tools: choose_research_action only. Follow the CURRENT option_id menu. "
                "Research skill references to database_search or reader describe operations performed "
                "by the runtime; never call those names directly. After discovery select an abstract; "
                "after reading it select full text or search again. The runtime supplies all material "
                "IDs and offsets. Do not invent a source, query, read_id, or artifact_id. "
                "You can finish with ResearchFinishDraft JSON at any time. When there are no options, "
                "finish using observed evidence or cards=[] with a no_evidence_reason."
            )
        names = (tuple(tool_names) if tool_names is not None else
                 self.tools.names + (("submit_evidence",) if self.config.enable_evidence_checkpoint else ()))
        note = "Available tools: " + ", ".join(names) + ". "
        if "browser" not in names:
            note += (
                "browser is unavailable. Web search is discovery only: do not attempt "
                "browser or pass a source_record_id to reader. Read only actual artifact IDs "
                "from available tools; prefer database/reference artifacts. Do not repeat "
                "web searches when there is no acquisition path. Finish with available evidence "
                "or explain the acquisition limitation."
            )
        if self.config.enable_evidence_checkpoint:
            note += (" After a useful Reader result, submit grounded partial cards with submit_evidence "
                     "before further exploration; accepted cards survive later execution failure. "
                     "This shares the existing tool-call budget and does not imply final Validator/Reviewer approval. "
                     "The original ResearchFinishDraft final response is still required.")
        return note


def _parse_finish(content):
    payload = json.loads(_extract_finish_json(content))
    warnings = []
    if isinstance(payload, dict) and isinstance(payload.get("cards"), list) and payload["cards"]:
        if payload.get("no_evidence_reason") is not None:
            warnings.append("moved conflicting no_evidence_reason to warnings: " + str(payload["no_evidence_reason"]))
            payload = {**payload, "no_evidence_reason": None}
    return ResearchFinishDraft.model_validate(payload), warnings


def _trusted_reads(trace) -> tuple[list[ReferenceReadResult], list[str]]:
    reads: list[ReferenceReadResult] = []
    warnings: list[str] = []
    for event in trace:
        observation = event.observation
        if (event.kind != "tool_result" or observation is None
                or observation.tool_name != "reader" or not observation.succeeded):
            continue
        rows = observation.payload.get("read_results", [observation.payload.get("read_result")])
        if not isinstance(rows, (list, tuple)):
            warnings.append("ignored malformed reader observation: read_results must be a list")
            continue
        for row in rows:
            try:
                reads.append(ReferenceReadResult.model_validate(row))
            except (ValidationError, ValueError, TypeError) as exc:
                warnings.append(f"ignored malformed reader observation: {_safe_error(exc)}")
    return reads, warnings


def _search_audit(trace) -> list[SearchExecution]:
    executions = {}
    for event in trace:
        observation = event.observation
        if event.kind != "tool_result" or observation is None:
            continue
        if observation.tool_name not in {"database_search", "structured_source_retrieval"}:
            continue
        payload = observation.payload
        bundle = payload.get("research_bundle") or payload.get("bundle") or {}
        rows = payload.get("search_executions", [])
        rows = rows if isinstance(rows, list) else []
        if isinstance(bundle, dict):
            nested = bundle.get("search_executions", [])
            rows = [*rows, *(nested if isinstance(nested, list) else [])]
        for row in rows:
            try:
                execution = SearchExecution.model_validate(row)
                executions[(execution.execution_id, execution.started_at)] = execution
            except (ValidationError, ValueError, TypeError):
                continue  # Malformed audit metadata must never discard evidence.
    return list(executions.values())


def _trusted_bundles(trace) -> tuple[list[ResearchBundle], list[str]]:
    bundles: list[ResearchBundle] = []
    warnings: list[str] = []
    for event in trace:
        observation = event.observation
        if event.kind != "tool_result" or observation is None:
            continue
        if not observation.succeeded:
            continue
        payload = observation.payload.get("research_bundle")
        if payload is None and observation.tool_name == "structured_source_retrieval":
            payload = observation.payload.get("bundle")
        if payload is None:
            continue
        try:
            bundles.append(ResearchBundle.model_validate(payload))
        except (ValidationError, ValueError, TypeError) as exc:
            warnings.append(f"ignored malformed research bundle: {_safe_error(exc)}")
    return bundles, warnings


def _turns_in_trace(trace) -> int:
    return sum(event.kind == "assistant_response" for event in trace)


def _partial(
    request, *, reads=None, bundles=None, warnings=None, steps=0, failures=None
) -> TaskResearchResult:
    return TaskResearchResult(
        task_id=request.research_task.task_id,
        novelty_point_id=request.novelty_point.point_id,
        status=TaskResearchStatus.PARTIAL,
        read_results=reads or [],
        research_bundles=bundles or [],
        warnings=warnings or [],
        steps_used=steps,
        execution_failures=failures or [],
    )


def _safe_error(exc: Exception) -> str:
    detail = f"{type(exc).__name__}: {exc}"
    if exc.__cause__ is not None:
        detail += f"; caused by {type(exc.__cause__).__name__}: {exc.__cause__}"
    return detail[:500]

def _project_novelty_point(point: Any) -> dict:
    """模型上下文投影：剔除 ``source_locations``。

    ``source_locations`` 是 PointExtractor 生成的、给人看的定位提示，**不保证
    逐字**——它会顺手清理 PDF 抽取残留（例如把 ``(model F _ { 2 } ) ) )`` 去掉）。
    但字符串本身带引号、还标着章节，模型会把它当成现成的引文直接写进 finish
    draft，随后必然被 EvidenceCardBuilder 判为 ungrounded，白白作废整轮检索。
    引文只应来自成功的 Reader 观测，因此这里不把它暴露给模型。
    """

    payload = point.model_dump(mode="json")
    payload.pop("source_locations", None)
    return payload


def _project_search_plan(plan: SearchPlan) -> dict:
    """模型上下文投影：只给最小语义（terms + expression），不给 ID/level/name/description/绑定。

    完整运行时 SearchPlan 只流向机器与审计；Researcher 提示词每轮工具循环
    都携带本投影，体积约省 30-40%。
    """

    return {
        "concepts": [
            {
                "terms": list(concept.terms),
                "role": concept.role,
                "alias": list(concept.alias),
            }
            for concept in plan.concepts
        ],
        "strategies": [
            {"expression": strategy.expression} for strategy in plan.strategies
        ],
    }


def _failure_scope(request):
    return FailureScope(paper_id=request.subject_paper_id, run_id=request.run_id,
        point_id=request.novelty_point.point_id, task_id=request.research_task.task_id)


def _execution_failure(request, exc, *, default=FailureCode.UNKNOWN, node="researcher.execute"):
    """Classify explicit exception facts, never infer a scientific negative."""
    from backend.env.model_client import (ModelCallBudgetExceeded, ModelContextAdmissionError,
        ModelTransportTimeout, ModelClientError)
    from ..core.tool_call_harness import ToolCallBudgetExhausted
    chain, seen = [], set()
    current = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        chain.append(current)
        current = current.__cause__
    code = default
    for error in reversed(chain):
        if isinstance(error, CodedFailure):
            code = error.code
        elif isinstance(error, ModelCallBudgetExceeded):
            code = FailureCode.MODEL_CALL_BUDGET
        elif isinstance(error, ToolCallBudgetExhausted):
            code = _budget_code(error.trace)
        elif isinstance(error, ModelContextAdmissionError):
            code = (FailureCode.MODEL_CONTEXT_LIMIT if error.code == 'CONTEXT_LIMIT_EXCEEDED'
                    else FailureCode.MODEL_CONTEXT_UNAVAILABLE)
        elif isinstance(error, (ModelTransportTimeout, TimeoutError)):
            code = FailureCode.MODEL_TIMEOUT
        elif isinstance(error, ModelClientError):
            code = model_failure_code(error)
        elif isinstance(error, (ValidationError, json.JSONDecodeError)):
            code = output_failure_code(error)
    return make_failure(code, scope=_failure_scope(request).model_copy(update={"node": node}),
        message='Task execution did not complete; preserved observations do not establish a scientific conclusion.',
        occurrence_id=type(exc).__name__)


def _observed_tool_failures(trace, request):
    failures = []
    for index, event in enumerate(trace):
        observation = event.observation
        if event.kind != 'tool_result' or observation is None:
            continue
        payload = observation.payload
        declared = [*payload.get('execution_issues', [])]
        if payload.get('failure'):
            declared.append(payload['failure'])
        for value in declared:
            try:
                failures.append(FailureEvent.model_validate(value))
            except (ValueError, TypeError):
                failures.append(make_failure(FailureCode.UNKNOWN, scope=_failure_scope(request),
                    message='Tool failure metadata did not satisfy the failure contract; observations are retained.',
                    occurrence_id=f'{index}-invalid-audit'))
        for execution in _search_audit([event]):
            if execution.failure is not None:
                declared.append(execution.failure.model_dump(mode="json"))
        errors = list(payload.get('read_errors', []))
        if not observation.succeeded and not declared and not errors:
            errors = [{'error_type':payload.get('error_type')}]
        for error_index, error in enumerate(errors):
            if error.get('error_type') == 'ReaderUnavailable' and declared:
                continue
            code = {'PermissionError':FailureCode.TOOL_SCOPE,
                    'ValidationError':FailureCode.TOOL_ARGUMENTS}.get(error.get('error_type'),FailureCode.UNKNOWN)
            if observation.tool_name == 'reader' and error.get('error_type') in {'OSError','FileNotFoundError'}:
                code = FailureCode.MATERIAL_UNAVAILABLE
            failures.append(make_failure(code, scope=_failure_scope(request).model_copy(update={
                'artifact_id':error.get('artifact_id')}),
                message='A tool operation failed; successful observations from this task remain available.',
                occurrence_id=f'{index}-{error_index}-{observation.tool_name}'))
    return failures


def _budget_code(trace):
    return next((event.failure_code for event in reversed(trace)
                 if event.failure_code is not None and failure_family(event.failure_code) == FailureCode.BUDGET_EXHAUSTED),
                FailureCode.BUDGET_EXHAUSTED)


def _harness_diagnostics(trace, request):
    # Historical observations, not unresolved blockers: a later choice may succeed.
    return [make_failure(event.failure_code,
                scope=_failure_scope(request).model_copy(update={"node": "harness.execute"}),
                message="Harness recorded a rejected decision or execution boundary.",
                occurrence_id=f"trace-{index}")
            for index, event in enumerate(trace) if event.kind == "error" and event.failure_code is not None]
