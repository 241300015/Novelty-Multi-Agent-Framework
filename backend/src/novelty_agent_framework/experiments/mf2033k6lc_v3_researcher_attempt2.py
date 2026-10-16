"""MF2033k6lC V3 Researcher Attempt 2 primary run.

Resume from verified MinerU and NoveltyPoint checkpoints. This experiment stops
after a successful primary run; the full-stack demo is deferred until Reviewer.
"""

from __future__ import annotations

import asyncio
import contextvars
import json
import subprocess
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from backend.env import (
    ChatMessage,
    ModelCallOptions,
    ModelClient,
    ModelClientError,
    ModelResponse,
)
from backend.env.model_client import _load_dev_env

from ..config import build_workflow, effective_safe_config, load_application_config
from ..schemas import NoveltyPoint, PaperInput, TaskResearchResult
from ..workflows import NoveltyWorkflow, NoveltyWorkflowServices
from .evidence_card_builder_live_smoke import _message, _trace

OUTPUT = Path("outputs/experiments/mf2033k6lc-v3-researcher-attempt2")
PAPER_INPUT = Path("outputs/MF2033k6lC/paper-input/others/paper.json")
POINTS_INPUT = Path("outputs/MF2033k6lC/novelty-points.json")
CONTENT_LIST = Path("outputs/MF2033k6lC/paper-input/content-list.json")
TASK_KEY = contextvars.ContextVar("attempt2_task_key", default="unknown")



class PersistedPointExtractor:
    def __init__(self, points: Sequence[NoveltyPoint]) -> None:
        self.points = tuple(points)

    def extract(self, digest, *, previous_brief, attempt):
        return self.points


class MeasuredClient:
    def __init__(self, delegate: ModelClient, role: str) -> None:
        self.delegate = delegate
        self.role = role
        self.calls: list[dict[str, Any]] = []

    @property
    def profile(self):
        return self.delegate.profile

    def complete(
        self,
        messages: Sequence[ChatMessage],
        *,
        options: ModelCallOptions | None = None,
    ) -> ModelResponse:
        started = time.perf_counter()
        try:
            response = self.delegate.complete(messages, options=options)
        except ModelClientError as exc:
            self._record_failure(messages, started, exc)
            if self.role != "coordinator" or "网络调用失败" not in str(exc):
                raise
            started = time.perf_counter()
            response = self.delegate.complete(messages, options=options)
        self._record(messages, response, started)
        return response

    async def acomplete(
        self,
        messages: Sequence[ChatMessage],
        *,
        options: ModelCallOptions | None = None,
    ) -> ModelResponse:
        started = time.perf_counter()
        response = await self.delegate.acomplete(messages, options=options)
        self._record(messages, response, started)
        return response

    def _record(self, messages, response, started) -> None:
        usage = dict(response.usage)
        details = usage.get("completion_tokens_details")
        self.calls.append(
            {
                "role": self.role,
                "task_key": TASK_KEY.get(),
                "turn_index": len(self.calls) + 1,
                "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
                "context_roles": [message.role for message in messages],
                "response_tool_calls": [
                    {
                        "id": call.id,
                        "name": call.name,
                        "arguments": dict(call.arguments),
                    }
                    for call in response.tool_calls
                ],
                "prompt_tokens": usage.get("prompt_tokens", 0),
                "completion_tokens": usage.get("completion_tokens", 0),
                "reasoning_tokens": (
                    details.get("reasoning_tokens", 0)
                    if isinstance(details, dict)
                    else 0
                ),
                "total_tokens": usage.get("total_tokens", 0),
            }
        )

    def _record_failure(self, messages, started, exc) -> None:
        self.calls.append(
            {
                "role": self.role,
                "task_key": TASK_KEY.get(),
                "turn_index": len(self.calls) + 1,
                "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
                "context_roles": [message.role for message in messages],
                "response_tool_calls": [],
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "reasoning_tokens": 0,
                "total_tokens": 0,
                "error": f"{type(exc).__name__}: {exc}",
                "retryable_provider_network_error": True,
            }
        )


class RecordingResearcher:
    def __init__(self, delegate) -> None:
        self.delegate = delegate
        self.requests = []
        self.results: list[TaskResearchResult] = []
        self.results_by_key: dict[str, TaskResearchResult] = {}
        self.elapsed_ms: dict[str, float] = {}

    async def ainvoke(self, request):
        key = f"{request.novelty_point.point_id}/{request.research_task.task_id}"
        token = TASK_KEY.set(key)
        self.requests.append(request)
        started = time.perf_counter()
        try:
            result = await self.delegate.ainvoke(request)
            self.results.append(result)
            self.results_by_key[key] = result
            return result
        finally:
            self.elapsed_ms[key] = round(
                (time.perf_counter() - started) * 1000, 3
            )
            TASK_KEY.reset(token)


def write_json(name: str, value: Any) -> None:
    OUTPUT.mkdir(parents=True, exist_ok=True)
    (OUTPUT / name).write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def token_total(calls: Sequence[dict[str, Any]]) -> int:
    return sum(int(call.get("total_tokens") or 0) for call in calls)


def failure_classes(warnings: Sequence[str]) -> list[str]:
    text = " ".join(warnings).lower()
    classes = []
    mapping = {
        "BUDGET_TOTAL": "total tool-call budget",
        "BUDGET_PER_TOOL": "tool-call budget exhausted",
        "BUDGET_READER_CHARS": "cumulative character budget",
        "MODEL_ERROR": "model call failed",
        "TOOL_ERROR": "tool error",
        "PROTOCOL_ERROR": "policy violation",
        "INVALID_FINISH_DRAFT": "invalid researchfinishdraft",
        "EVIDENCE_BUILD_ERROR": "evidence builder failed",
        "NO_EVIDENCE": "no evidence",
        "WORKFLOW_ERROR": "workflow",
    }
    for name, needle in mapping.items():
        if needle in text:
            classes.append(name)
    return classes or (["OTHER"] if warnings else [])


def task_trace_summary(trace, registry, reader_default: int) -> dict[str, Any]:
    attempted = [
        call
        for event in trace
        if event.kind == "assistant_response" and event.message is not None
        for call in event.message.tool_calls
    ]
    executed = [
        event.tool_call
        for event in trace
        if event.kind == "tool_call" and event.tool_call is not None
    ]
    errors = [
        event.detail
        for event in trace
        if event.kind == "error" and event.detail
    ]
    attempted_counts = Counter(call.name for call in attempted)
    executed_counts = Counter(call.name for call in executed)
    reader_attempts = [call for call in attempted if call.name == "reader"]
    requested_chars = []
    for call in reader_attempts:
        try:
            canonical = registry.validate_arguments("reader", call.arguments)
            requested_chars.append(canonical.max_chars)
        except Exception:
            requested_chars.append(call.arguments.get("max_chars", reader_default))
    actual_chars = []
    for event in trace:
        if (
            event.kind != "tool_result"
            or event.observation is None
            or event.observation.tool_name != "reader"
            or not event.observation.succeeded
        ):
            continue
        read = event.observation.payload.get("read_result", {})
        start, end = read.get("char_start"), read.get("char_end")
        if isinstance(start, int) and isinstance(end, int):
            actual_chars.append(max(0, end - start))
    rejected_counts = {
        name: max(0, attempted_counts[name] - executed_counts[name])
        for name in attempted_counts
    }
    return {
        "attempted_tool_sequence": [call.name for call in attempted],
        "executed_tool_sequence": [call.name for call in executed],
        "attempted_per_tool_counts": dict(attempted_counts),
        "executed_per_tool_counts": dict(executed_counts),
        "rejected_per_tool_counts": rejected_counts,
        "budget_rejections": [item for item in errors if "budget" in item],
        "protocol_rejections": [
            item for item in errors if "policy" in item or "multiple" in item
        ],
        "reader_attempted": len(reader_attempts),
        "reader_executed": executed_counts["reader"],
        "reader_rejected": rejected_counts.get("reader", 0),
        "requested_chars": requested_chars,
        "actual_chars": actual_chars,
        "actual_total_read_chars": sum(actual_chars),
    }


async def run() -> dict[str, Any]:
    _load_dev_env()
    config = load_application_config()
    workflow_settings = config.project.workflow.model_copy(
        update={"max_rounds": 1, "max_concurrency": 1}
    )
    config = config.model_copy(
        update={
            "project": config.project.model_copy(
                update={"workflow": workflow_settings}
            )
        }
    )
    paper = PaperInput.model_validate_json(PAPER_INPUT.read_text(encoding="utf-8"))
    point_payload = json.loads(POINTS_INPUT.read_text(encoding="utf-8"))
    points = [
        NoveltyPoint.model_validate(item)
        for item in point_payload["novelty_points"]
    ]
    content_list = json.loads(CONTENT_LIST.read_text(encoding="utf-8"))
    if paper.metadata.get("source") != "mineru":
        raise RuntimeError("persisted PaperInput is not a verified MinerU artifact")

    built = build_workflow(config)
    researcher = built.services.task_researcher
    research_client = MeasuredClient(researcher.model_client, "researcher")
    researcher.model_client = research_client
    researcher.harness.model_client = research_client

    database = researcher.tools.get("database_search")
    planner = next(iter(database.tools_by_source.values())).search_planner
    planner_client = MeasuredClient(planner._client(), "search_planner")
    planner.model_client = planner_client
    coordinator = built.services.coordinator
    coordinator_client = MeasuredClient(coordinator._client(), "coordinator")
    coordinator.model_client = coordinator_client

    traces: dict[str, tuple] = {}
    harness_failures: dict[str, str] = {}
    original_run = researcher.harness.run

    async def recording_harness(**kwargs):
        scope = kwargs["scope"]
        key = f"{scope.novelty_point.point_id}/{scope.research_task.task_id}"
        try:
            result = await original_run(**kwargs)
            traces[key] = result.trace
            return result
        except Exception as exc:
            traces[key] = tuple(getattr(exc, "trace", ()))
            harness_failures[key] = str(exc)
            raise

    researcher.harness.run = recording_harness
    recording = RecordingResearcher(researcher)
    workflow = NoveltyWorkflow(
        NoveltyWorkflowServices(
            coordinator=coordinator,
            task_researcher=recording,
            search_planner=built.services.search_planner,
            point_extractor=PersistedPointExtractor(points),
        ),
        built.config,
    )

    started_at = datetime.now(timezone.utc).isoformat()
    started = time.perf_counter()
    result = await workflow.arun(paper)
    elapsed_ms = round((time.perf_counter() - started) * 1000, 3)

    trace_root = OUTPUT / "traces"
    for key, trace in traces.items():
        path = trace_root / f"{key}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "".join(
                json.dumps(_trace(index, event), ensure_ascii=False) + "\n"
                for index, event in enumerate(trace, 1)
            ),
            encoding="utf-8",
        )

    research_calls = defaultdict(list)
    planner_calls = defaultdict(list)
    for call in research_client.calls:
        research_calls[call["task_key"]].append(call)
    for call in planner_client.calls:
        planner_calls[call["task_key"]].append(call)

    task_summaries = []
    limits = config.researcher.harness.per_tool_limits
    total_limit = config.researcher.harness.max_total_tool_calls
    read_limit = config.researcher.tools.reader.max_total_read_chars
    budget_violations = 0
    for request in recording.requests:
        key = f"{request.novelty_point.point_id}/{request.research_task.task_id}"
        task_result = recording.results_by_key.get(key)
        trace_summary = task_trace_summary(
            traces.get(key, ()),
            researcher.tools,
            config.researcher.tools.reader.default_chars_per_read,
        )
        executed_counts = trace_summary["executed_per_tool_counts"]
        violations = []
        if sum(executed_counts.values()) > total_limit:
            violations.append("max_total_tool_calls")
        violations.extend(
            name for name, count in executed_counts.items() if count > limits.get(name, count)
        )
        if trace_summary["actual_total_read_chars"] > read_limit:
            violations.append("max_total_read_chars")
        budget_violations += len(violations)
        warnings = (
            list(task_result.warnings)
            if task_result
            else [harness_failures.get(key, "missing result")]
        )
        task_summaries.append(
            {
                "task_key": key,
                "task_id": request.research_task.task_id,
                "novelty_point_id": request.novelty_point.point_id,
                "language": request.research_task.language,
                "task_type": request.research_task.task_type,
                "status": task_result.status.value if task_result else "missing",
                "steps_used": task_result.steps_used if task_result else 0,
                "elapsed_ms": recording.elapsed_ms.get(key),
                **trace_summary,
                "configured_max_total_read_chars": read_limit,
                "harness_failure": harness_failures.get(key),
                "trusted_read_count": len(task_result.read_results) if task_result else 0,
                "trusted_read_chars": sum(
                    len(item.text) for item in task_result.read_results
                ) if task_result else 0,
                "evidence_count": len(task_result.evidence) if task_result else 0,
                "evidence_card_count": len(task_result.evidence_cards) if task_result else 0,
                "warnings": warnings,
                "failure_classifications": failure_classes(warnings),
                "budget_violations": violations,
                "researcher_model_calls": len(research_calls[key]),
                "researcher_token_usage": research_calls[key],
                "researcher_total_tokens": token_total(research_calls[key]),
                "search_planner_calls": len(planner_calls[key]),
                "search_planner_token_usage": planner_calls[key],
                "search_planner_total_tokens": token_total(planner_calls[key]),
            }
        )

    all_evidence = [
        evidence for task_result in recording.results for evidence in task_result.evidence
    ]
    all_cards = [
        card for task_result in recording.results for card in task_result.evidence_cards
    ]
    planned = len(result.brief.research_tasks)
    dispatched = len(recording.requests)
    fan_in = len(recording.results) == dispatched == planned
    pipeline_pass = fan_in and result.report is not None
    workflow_summary = {
        "planned_task_count": planned,
        "dispatched_task_count": dispatched,
        "result_count": len(recording.results),
        "completed_task_count": sum(item.status.value == "completed" for item in recording.results),
        "partial_task_count": sum(item.status.value == "partial" for item in recording.results),
        "failed_task_count": sum(item.status.value == "failed" for item in recording.results),
        "empty_evidence_task_count": sum(not item.evidence for item in recording.results),
        "tasks_with_reader": sum(item["reader_executed"] > 0 for item in task_summaries),
        "tasks_with_evidence": sum(item["evidence_count"] > 0 for item in task_summaries),
        "tasks_with_evidence_cards": sum(item["evidence_card_count"] > 0 for item in task_summaries),
        "fan_in_complete": fan_in,
        "synthesize_complete": result.report is not None,
        "pipeline_status": "PASS" if pipeline_pass else "FAIL",
        "budget_violation_count": budget_violations,
        "workflow_elapsed_ms": elapsed_ms,
        "validator_mode": "experimental_passthrough",
        "reviewer_enabled": False,
        "full_stack_rerun": "DEFERRED_UNTIL_REVIEWER" if pipeline_pass else "NOT_APPLICABLE",
    }
    token_summary = {
        "coordinator_tokens": token_total(coordinator_client.calls),
        "point_extractor_tokens": 0,
        "researcher_tokens": token_total(research_client.calls),
        "search_planner_tokens": token_total(planner_client.calls),
        "grand_total_tokens": token_total(
            coordinator_client.calls + research_client.calls + planner_client.calls
        ),
        "per_task": {
            item["task_key"]: {
                "researcher": item["researcher_total_tokens"],
                "search_planner": item["search_planner_total_tokens"],
            }
            for item in task_summaries
        },
    }
    processing_summary = {
        "paper_id": paper.paper_id,
        "actual_parser": paper.metadata["source"],
        "start_mode": "persisted_mineru",
        "point_extractor_mode": "persisted_checkpoint",
        "mineru_reexecuted": False,
        "page_count": content_list["page_count"],
        "full_text_chars": len(paper.full_text),
        "reference_count": len(paper.references),
    }
    metadata = {
        "git_sha": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], text=True
        ).strip(),
        "timestamp": started_at,
        "experiment": "MF2033k6lC V3",
        "researcher_attempt": 2,
        "start_mode": "persisted_mineru",
        "point_extractor_mode": "persisted_checkpoint",
        "models": {
            "researcher": config.researcher.model.alias,
            "search_planner": config.search_planner.model.alias,
            "coordinator": config.coordinator.model.alias,
            "point_extractor": config.point_extractor.model.alias,
        },
        "config_paths": "split example config defaults",
        "workflow_overrides": {"max_rounds": 1, "max_concurrency": 1},
        "provider_retry_policy": (
            "one coordinator retry after a ModelClientError network failure"
        ),
        "prior_run": {
            "status": "failed",
            "stage": "synthesize_report",
            "classification": "PROVIDER_TRANSIENT_NETWORK_TIMEOUT",
        },
        "validator_mode": "experimental_passthrough",
        "reviewer_mode": "disabled",
    }
    store = researcher.evidence_builder.reference_store
    write_json("experiment_metadata.json", metadata)
    write_json("effective_config.json", effective_safe_config(config))
    write_json("processing_summary.json", processing_summary)
    write_json("point_extractor_summary.json", {"mode": "persisted", "point_count": len(points)})
    write_json("workflow_summary.json", workflow_summary)
    write_json("task_summary.json", task_summaries)
    write_json("token_summary.json", token_summary)
    write_json("manifest_snapshot.json", store.load_manifest(paper.paper_id).model_dump(mode="json"))
    write_json("evidence_snapshot.json", [item.model_dump(mode="json") for item in all_evidence])
    write_json("evidence_cards_snapshot.json", [item.model_dump(mode="json") for item in all_cards])
    write_json("workflow_result.json", result.model_dump(mode="json"))
    raw = {
        "metadata": metadata,
        "processing": processing_summary,
        "workflow": workflow_summary,
        "tasks": task_summaries,
        "tokens": token_summary,
    }
    write_json("raw_experiment_result.json", raw)
    return raw


def main() -> None:
    print(json.dumps(asyncio.run(run()), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
