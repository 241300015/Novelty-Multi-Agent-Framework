"""Issue #7 live：禁用 WebSearch/Browser 的单 Task 正式检索实验。"""

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
from ..core.search_plan_expression import parse_search_plan_expression
from ..schemas import NoveltyPoint, PaperInput, ResearchFinishDraft, TaskResearchRequest
from ..tools import ResearcherToolRegistry
from .evidence_card_builder_live_smoke import _trace
from .mf2033k6lc_v3_researcher_attempt2 import MeasuredClient, TASK_KEY, token_total

OUTPUT = Path("outputs/experiments/issue7-single-task-database-only-live")
PAPER_INPUT = Path("outputs/MF2033k6lC/paper-input/others/paper.json")
POINTS_INPUT = Path("outputs/MF2033k6lC/novelty-points.json")
SELECTED_POINT_ID = "NP-1"
SELECTED_TASK_ID = "T-1"
_SENSITIVE_KEYS = {"api_key", "authorization", "cookie", "password", "secret", "token"}


class FailingLegacyPlanner:
    def __init__(self) -> None:
        self.calls = 0

    def plan(self, *args: Any, **kwargs: Any) -> None:
        self.calls += 1
        raise AssertionError("legacy planner path must not be called")


def write_json(name: str, value: Any) -> None:
    OUTPUT.mkdir(parents=True, exist_ok=True)
    (OUTPUT / name).write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def sanitize_telemetry(value: Any) -> Any:
    """递归移除候选载荷中可能出现的凭据字段。"""

    if isinstance(value, dict):
        return {
            str(key): (
                "<redacted>"
                if any(marker in str(key).lower() for marker in _SENSITIVE_KEYS)
                else sanitize_telemetry(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [sanitize_telemetry(item) for item in value]
    if isinstance(value, tuple):
        return [sanitize_telemetry(item) for item in value]
    return value


async def run() -> dict[str, Any]:
    _load_dev_env()
    config = load_application_config()
    paper = PaperInput.model_validate_json(PAPER_INPUT.read_text(encoding="utf-8"))
    point_payload = json.loads(POINTS_INPUT.read_text(encoding="utf-8"))
    point = next(
        NoveltyPoint.model_validate(item)
        for item in point_payload["novelty_points"]
        if item["point_id"] == SELECTED_POINT_ID
    )

    built = build_workflow(config)
    brief = built.services.coordinator.plan(paper, points=[point], attempt=1)
    task = next(
        item
        for item in brief.research_tasks
        if item.novelty_point_id == SELECTED_POINT_ID
        and item.task_id == SELECTED_TASK_ID
    )

    planner = built.services.search_planner
    planner_client = MeasuredClient(planner._client(), "search_planner")
    planner.model_client = planner_client
    planner_attempts: list[dict[str, Any]] = []
    original_complete_json = planner._complete_json

    def recording_complete_json(*, point, task, retry_reason):
        attempt: dict[str, Any] = {
            "attempt": len(planner_attempts) + 1,
            "retry_reason": sanitize_telemetry(retry_reason),
        }
        try:
            candidate = original_complete_json(
                point=point, task=task, retry_reason=retry_reason
            )
            attempt["candidate"] = sanitize_telemetry(candidate)
            return candidate
        except Exception as exc:
            attempt["error"] = sanitize_telemetry(f"{type(exc).__name__}: {exc}")
            raise
        finally:
            planner_attempts.append(attempt)

    planner._complete_json = recording_complete_json
    try:
        plan = planner.plan(point, task)
    except Exception as exc:
        summary = {
            "experiment": "Issue #7 single-task database-only live",
            "started_at": datetime.now(timezone.utc).isoformat(),
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "git_sha": subprocess.check_output(
                ["git", "rev-parse", "HEAD"], text=True
            ).strip(),
            "phase": "search_planner",
            "status": "failed",
            "error": sanitize_telemetry(f"{type(exc).__name__}: {exc}"),
            "planner_call_count": len(planner_client.calls),
            "planner_attempts": planner_attempts,
            "tokens": {"search_planner": token_total(planner_client.calls)},
        }
        write_json("summary.json", summary)
        write_json("planner_attempts.json", planner_attempts)
        write_json("effective_config.json", effective_safe_config(config))
        write_json("model_calls.json", {"search_planner": planner_client.calls})
        return summary
    defined = {concept.concept_id for concept in plan.concepts}
    expression_checks = [
        {
            "strategy_id": strategy.strategy_id,
            "level": strategy.level,
            "expression": strategy.expression,
            "tokens": list(
                parse_search_plan_expression(
                    strategy.expression, defined_concepts=defined
                )
            ),
            "valid": True,
        }
        for strategy in plan.strategies
    ]
    request = TaskResearchRequest(
        subject_paper_id=paper.paper_id,
        run_id="issue7-single-task-database-only-live",
        novelty_point=point,
        research_task=task,
        search_plan=plan,
    )

    researcher = built.services.task_researcher
    database = researcher.tools.get("database_search")
    reader = researcher.tools.get("reader")
    restricted_registry = ResearcherToolRegistry([database, reader])
    researcher.tools = restricted_registry
    researcher.harness.registry = restricted_registry

    legacy_guards: list[FailingLegacyPlanner] = []
    for retrieval in database.tools_by_source.values():
        guard = FailingLegacyPlanner()
        retrieval.search_planner = guard
        legacy_guards.append(guard)

    researcher_client = MeasuredClient(researcher.model_client, "researcher")
    researcher.model_client = researcher_client
    researcher.harness.model_client = researcher_client
    captured_trace = ()
    harness_result = None
    original_run = researcher.harness.run

    async def recording_harness(**kwargs: Any):
        nonlocal captured_trace, harness_result
        try:
            harness_result = await original_run(**kwargs)
            captured_trace = harness_result.trace
            return harness_result
        except Exception as exc:
            captured_trace = tuple(getattr(exc, "trace", ()))
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

    tool_events = [
        event for event in captured_trace if event.kind == "tool_call" and event.tool_call
    ]
    tool_names = [event.tool_call.name for event in tool_events]
    observations = [
        event.observation
        for event in captured_trace
        if event.kind == "tool_result" and event.observation is not None
    ]
    database_observations = [
        item for item in observations if item.tool_name == "database_search"
    ]
    warnings = list(result.warnings)
    warning_text = " ".join(warnings + [item.error or "" for item in observations])
    draft = None
    draft_error = None
    if harness_result is not None:
        try:
            draft = ResearchFinishDraft.model_validate_json(
                harness_result.final_content or ""
            )
        except Exception as exc:
            draft_error = f"{type(exc).__name__}: {exc}"

    summary = {
        "experiment": "Issue #7 single-task database-only live",
        "started_at": started_at,
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "git_sha": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], text=True
        ).strip(),
        "selected_task": {
            "point_id": point.point_id,
            "task_id": task.task_id,
            "task_type": task.task_type,
            "language": task.language,
        },
        "registered_tools": list(restricted_registry.names),
        "disabled_tools": ["web_search", "browser"],
        "planner_call_count": len(planner_client.calls),
        "planner_attempts": planner_attempts,
        "search_plan": plan.model_dump(mode="json"),
        "expression_checks": expression_checks,
        "all_expressions_valid": all(item["valid"] for item in expression_checks),
        "strategy_levels": [item.level for item in plan.strategies],
        "executed_tool_sequence": tool_names,
        "tool_counts": {
            name: tool_names.count(name)
            for name in ("database_search", "reader", "web_search", "browser")
        },
        "database_results": [
            {
                "succeeded": item.succeeded,
                "error": item.error,
                "result_count": len(
                    item.payload.get("database_search_result", {}).get("results", [])
                ),
                "warnings": item.payload.get("database_search_result", {}).get(
                    "warnings", []
                ),
            }
            for item in database_observations
        ],
        "unsupported_literal_token_seen": "unsupported literal token" in warning_text,
        "legacy_planner_calls": sum(guard.calls for guard in legacy_guards),
        "finish_reached": harness_result is not None,
        "draft_valid": draft is not None,
        "draft_error": draft_error,
        "status": result.status.value,
        "warnings": warnings,
        "read_count": len(result.read_results),
        "evidence_count": len(result.evidence),
        "evidence_card_count": len(result.evidence_cards),
        "steps_used": result.steps_used,
        "elapsed_ms": elapsed_ms,
        "tokens": {
            "search_planner": token_total(planner_client.calls),
            "researcher": token_total(researcher_client.calls),
            "total": token_total(planner_client.calls + researcher_client.calls),
        },
    }

    OUTPUT.mkdir(parents=True, exist_ok=True)
    (OUTPUT / "trace.jsonl").write_text(
        "".join(
            json.dumps(_trace(index, event), ensure_ascii=False) + "\n"
            for index, event in enumerate(captured_trace, 1)
        ),
        encoding="utf-8",
    )
    write_json("summary.json", summary)
    write_json("planner_attempts.json", planner_attempts)
    write_json("task_request.json", request.model_dump(mode="json"))
    write_json("task_result.json", result.model_dump(mode="json"))
    write_json("effective_config.json", effective_safe_config(config))
    write_json(
        "model_calls.json",
        {
            "search_planner": planner_client.calls,
            "researcher": researcher_client.calls,
        },
    )
    return summary


def main() -> None:
    print(json.dumps(asyncio.run(run()), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
