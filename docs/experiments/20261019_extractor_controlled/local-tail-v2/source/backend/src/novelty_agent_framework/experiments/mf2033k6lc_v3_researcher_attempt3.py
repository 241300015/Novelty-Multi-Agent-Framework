"""Attempt 3: one real ResearchTask through grounded EvidenceCard building."""

from __future__ import annotations

import asyncio
import json
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from backend.env.model_client import _load_dev_env

from ..config import build_workflow, effective_safe_config, load_application_config
from ..schemas import NoveltyPoint, PaperInput, ResearchFinishDraft, TaskResearchRequest
from .evidence_card_builder_live_smoke import _trace
from .mf2033k6lc_v3_researcher_attempt2 import MeasuredClient, TASK_KEY, token_total

OUTPUT = Path("outputs/experiments/mf2033k6lc-v3-researcher-attempt3")
PAPER_INPUT = Path("outputs/MF2033k6lC/paper-input/others/paper.json")
POINTS_INPUT = Path("outputs/MF2033k6lC/novelty-points.json")
SELECTED_POINT_ID = "NP-1"
SELECTED_TASK_ID = "T-1"


def write_json(name: str, value: Any) -> None:
    OUTPUT.mkdir(parents=True, exist_ok=True)
    (OUTPUT / name).write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def observations(trace, tool_name: str, *, successful_only: bool = False):
    values = [
        event.observation
        for event in trace
        if event.kind == "tool_result"
        and event.observation is not None
        and event.observation.tool_name == tool_name
    ]
    if successful_only:
        return [item for item in values if item.succeeded]
    return values


def calls(trace, *, attempted: bool) -> list:
    if attempted:
        return [
            call
            for event in trace
            if event.kind == "assistant_response" and event.message is not None
            for call in event.message.tool_calls
        ]
    return [
        event.tool_call
        for event in trace
        if event.kind == "tool_call" and event.tool_call is not None
    ]


class FailingLegacyPlanner:
    """Proof guard: database retrieval must consume the request plan."""

    def plan(self, *args, **kwargs):
        raise AssertionError("legacy planner path must not be called")


def summarize_web_rounds(trace) -> list[dict[str, Any]]:
    executed = calls(trace, attempted=False)
    web_indexes = [index for index, call in enumerate(executed) if call.name == "web_search"]
    browser_results = observations(trace, "browser", successful_only=True)
    reader_results = observations(trace, "reader", successful_only=True)
    rounds = []
    for round_index, start in enumerate(web_indexes):
        end = web_indexes[round_index + 1] if round_index + 1 < len(web_indexes) else len(executed)
        segment = executed[start + 1 : end]
        browser_call = next((call for call in segment if call.name == "browser"), None)
        reader_call = next((call for call in segment if call.name == "reader"), None)
        selected = browser_call.arguments.get("source_record_id") if browser_call else None
        matching_browsers = [
            item
            for item in browser_results
            if item.arguments.get("source_record_id") == selected
        ]
        artifact_ids = [
            artifact["artifact_id"]
            for item in matching_browsers
            for artifact in item.payload.get("browser_result", {}).get("artifacts", [])
        ]
        matching_reads = [
            item
            for item in reader_results
            if item.arguments.get("artifact_id") in artifact_ids
        ]
        trusted_chars = sum(
            max(
                0,
                item.payload["read_result"]["char_end"]
                - item.payload["read_result"]["char_start"],
            )
            for item in matching_reads
        )
        rounds.append(
            {
                "search_index": round_index + 1,
                "source_record_selected": selected,
                "browser_called_before_next_search": browser_call is not None,
                "artifact_created": bool(artifact_ids),
                "artifact_ids": artifact_ids,
                "reader_called": reader_call is not None,
                "trusted_read_created": bool(matching_reads),
                "trusted_read_chars": trusted_chars,
                "evaluation_result": (
                    "reader_text_available_before_next_search"
                    if matching_reads and end < len(executed)
                    else "reader_text_available_before_finish"
                    if matching_reads
                    else "no_trusted_reader_text"
                ),
            }
        )
    return rounds


def summarize_database(trace) -> list[dict[str, Any]]:
    executed = calls(trace, attempted=False)
    values = []
    for observation in observations(trace, "database_search"):
        result = observation.payload.get("database_search_result", {})
        items = result.get("results", [])
        artifact_ids = [
            artifact_id for item in items for artifact_id in item.get("artifact_ids", [])
        ]
        db_index = next(
            (
                index
                for index, call in enumerate(executed)
                if call.name == "database_search"
                and call.arguments.get("source_id") == result.get("source_id")
            ),
            -1,
        )
        later = executed[db_index + 1 :] if db_index >= 0 else []
        reader_used = any(
            call.name == "reader" and call.arguments.get("artifact_id") in artifact_ids
            for call in later
        )
        web_used = any(call.name == "web_search" for call in later)
        values.append(
            {
                "database_source": result.get("source_id"),
                "candidate_count": len(items),
                "artifact_ids_available": artifact_ids,
                "reader_used_directly": reader_used,
                "fallback_to_web": web_used,
                "fallback_reason": (
                    "model broadened recall after database results"
                    if web_used
                    else None
                ),
                "warnings": result.get("warnings", []),
            }
        )
    return values


def summarize_reads(trace) -> list[dict[str, Any]]:
    values = []
    for observation in observations(trace, "reader"):
        read = observation.payload.get("read_result", {})
        start, end = read.get("char_start"), read.get("char_end")
        values.append(
            {
                "artifact_id": read.get("artifact_id") or observation.arguments.get("artifact_id"),
                "char_start": start,
                "char_end": end,
                "trusted_read_chars": (
                    max(0, end - start)
                    if isinstance(start, int) and isinstance(end, int)
                    else 0
                ),
                "reader_success": observation.succeeded,
                "read_id": read.get("read_id"),
            }
        )
    return values


async def run() -> dict[str, Any]:
    _load_dev_env()
    config = load_application_config()
    paper = PaperInput.model_validate_json(PAPER_INPUT.read_text(encoding="utf-8"))
    point_payload = json.loads(POINTS_INPUT.read_text(encoding="utf-8"))
    points = [
        NoveltyPoint.model_validate(item)
        for item in point_payload["novelty_points"]
    ]
    point = next(item for item in points if item.point_id == SELECTED_POINT_ID)
    built = build_workflow(config)
    planner = built.services.search_planner
    planner_client = MeasuredClient(planner._client(), "search_planner")
    planner.model_client = planner_client
    brief = built.services.coordinator.plan(paper, points=points, attempt=1)
    task = next(
        item
        for item in brief.research_tasks
        if item.novelty_point_id == SELECTED_POINT_ID and item.task_id == SELECTED_TASK_ID
    )
    request = TaskResearchRequest(
        subject_paper_id=paper.paper_id,
        run_id="v3-researcher-attempt3",
        novelty_point=point,
        research_task=task,
        search_plan=built.services.search_planner.plan(point, task),
    )

    researcher = built.services.task_researcher
    researcher_client = MeasuredClient(researcher.model_client, "researcher")
    researcher.model_client = researcher_client
    researcher.harness.model_client = researcher_client
    database = researcher.tools.get("database_search")
    for retrieval in database.tools_by_source.values():
        retrieval.search_planner = FailingLegacyPlanner()

    captured_trace = ()
    harness_result = None
    harness_failure = None
    original_run = researcher.harness.run

    async def recording_harness(**kwargs):
        nonlocal captured_trace, harness_result, harness_failure
        try:
            harness_result = await original_run(**kwargs)
            captured_trace = harness_result.trace
            return harness_result
        except Exception as exc:
            captured_trace = tuple(getattr(exc, "trace", ()))
            harness_failure = f"{type(exc).__name__}: {exc}"
            raise

    researcher.harness.run = recording_harness
    started_at = datetime.now(timezone.utc).isoformat()
    started = time.perf_counter()
    token = TASK_KEY.set(f"{SELECTED_POINT_ID}/{SELECTED_TASK_ID}")
    try:
        result = await researcher.ainvoke(request)
    finally:
        TASK_KEY.reset(token)
    elapsed_ms = round((time.perf_counter() - started) * 1000, 3)

    trace_path = OUTPUT / "trace.jsonl"
    trace_path.parent.mkdir(parents=True, exist_ok=True)
    trace_path.write_text(
        "".join(
            json.dumps(_trace(index, event), ensure_ascii=False) + "\n"
            for index, event in enumerate(captured_trace, 1)
        ),
        encoding="utf-8",
    )
    attempted = calls(captured_trace, attempted=True)
    executed = calls(captured_trace, attempted=False)
    attempted_names = [call.name for call in attempted]
    executed_names = [call.name for call in executed]
    consecutive_web = any(
        left == right == "web_search"
        for left, right in zip(executed_names, executed_names[1:])
    )
    reader_observations = observations(captured_trace, "reader", successful_only=True)
    trusted_reads = [item.payload["read_result"] for item in reader_observations]
    draft = None
    draft_error = None
    if harness_result is not None:
        try:
            draft = ResearchFinishDraft.model_validate_json(
                harness_result.final_content or ""
            )
        except Exception as exc:
            draft_error = f"{type(exc).__name__}: {exc}"
    quotes = [quote.quote for card in draft.cards for quote in card.quotes] if draft else []
    exact_quotes = [quote for quote in quotes if any(quote in read["text"] for read in trusted_reads)]
    builder_error = next(
        (warning for warning in result.warnings if "evidence builder failed" in warning.lower()),
        None,
    )
    web_rounds = summarize_web_rounds(captured_trace)
    database_rounds = summarize_database(captured_trace)
    reads = summarize_reads(captured_trace)
    artifact_ids = sorted(
        {
            artifact_id
            for round_summary in web_rounds
            for artifact_id in round_summary["artifact_ids"]
        }
        | {
            artifact_id
            for round_summary in database_rounds
            for artifact_id in round_summary["artifact_ids_available"]
        }
    )
    behavior_pass = not consecutive_web and all(
        item["browser_called_before_next_search"]
        and item["artifact_created"]
        and item["reader_called"]
        and item["trusted_read_created"]
        for item in web_rounds
    )
    acquisition_pass = bool(trusted_reads and artifact_ids)
    grounding_pass = bool(quotes and len(exact_quotes) == len(quotes) and not builder_error)
    attempt_success = bool(result.evidence_cards)
    total_tokens = token_total(researcher_client.calls + planner_client.calls)
    summary = {
        "experiment": "MF2033k6lC V3 Researcher Attempt 3",
        "timestamp": started_at,
        "git_sha": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
        "selected_task": {
            "task_key": f"{SELECTED_POINT_ID}/{SELECTED_TASK_ID}",
            "language": task.language,
            "task_type": task.task_type,
            "selection_reason": (
                "previous run completed WebSearch-Browser-Reader and produced one draft "
                "card rejected only for an ungrounded paraphrased quote"
            ),
        },
        "planner_call_count": len(planner_client.calls),
        "search_plan": request.search_plan.model_dump(mode="json"),
        "task_research_request": request.model_dump(mode="json"),
        "attempted_tool_sequence": attempted_names,
        "executed_tool_sequence": executed_names,
        "tool_counts": {
            name: executed_names.count(name)
            for name in ("database_search", "web_search", "browser", "reader")
        },
        "finish_reached": harness_result is not None,
        "consecutive_web_search_violation": consecutive_web,
        "web_search_rounds": web_rounds,
        "database_search_rounds": database_rounds,
        "reader_results": reads,
        "artifact_ids": artifact_ids,
        "trusted_read_chars": sum(len(item["text"]) for item in trusted_reads),
        "draft": {
            "valid": draft is not None,
            "error": draft_error,
            "card_count": len(draft.cards) if draft else 0,
            "quote_count": len(quotes),
            "exact_reader_quote_count": len(exact_quotes),
            "all_quotes_exact": bool(quotes) and len(exact_quotes) == len(quotes),
        },
        "builder": {
            "called": draft is not None,
            "success": draft is not None and builder_error is None,
            "rejection": builder_error is not None,
            "error": builder_error,
            "evidence_count": len(result.evidence),
            "evidence_card_count": len(result.evidence_cards),
        },
        "levels": {
            "behavior_policy": "PASS" if behavior_pass else "FAIL",
            "acquisition": "PASS" if acquisition_pass else "FAIL",
            "grounding": "PASS" if grounding_pass else "FAIL",
            "attempt3": "SUCCESS" if attempt_success else "NOT_PROVEN",
        },
        "status": result.status.value,
        "warnings": list(result.warnings),
        "harness_failure": harness_failure,
        "elapsed_ms": elapsed_ms,
        "tokens": {
            "researcher": token_total(researcher_client.calls),
            "search_planner": token_total(planner_client.calls),
            "total": total_tokens,
        },
    }
    store = researcher.evidence_builder.reference_store
    write_json("summary.json", summary)
    write_json("task_result.json", result.model_dump(mode="json"))
    write_json("manifest_snapshot.json", store.load_manifest(paper.paper_id).model_dump(mode="json"))
    write_json("reader_outputs.json", trusted_reads)
    write_json("selected_artifacts.json", artifact_ids)
    write_json(
        "token_summary.json",
        {
            "researcher_calls": researcher_client.calls,
            "search_planner_calls": planner_client.calls,
            **summary["tokens"],
        },
    )
    write_json("effective_config.json", effective_safe_config(config))
    return summary


def main() -> None:
    print(json.dumps(asyncio.run(run()), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
