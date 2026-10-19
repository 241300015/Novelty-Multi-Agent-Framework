"""Controlled single-pass prompt pretest for the six MF2033k6lC tasks."""

from __future__ import annotations

import asyncio
import json
import subprocess
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from backend.env import RenderedPrompt
from backend.env.model_client import _load_dev_env

from ..config import build_workflow, effective_safe_config, load_application_config
from ..schemas import (
    NoveltyPoint,
    PaperInput,
    ResearchFinishDraft,
    TaskResearchRequest,
)
from .evidence_card_builder_live_smoke import _trace
from .mf2033k6lc_v3_researcher_attempt2 import (
    MeasuredClient,
    TASK_KEY,
    token_total,
)

OUTPUT = Path(
    "outputs/experiments/mf2033k6lc-v3-attempt2-single-pass-prompt-pretest"
)
PAPER_INPUT = Path("outputs/MF2033k6lC/paper-input/others/paper.json")
POINTS_INPUT = Path("outputs/MF2033k6lC/novelty-points.json")
ORIGINAL_TASK_SUMMARY = Path(
    "outputs/experiments/MF2033k6lC_V3_Researcher_Attempt2_Taskbook/"
    "mf2033k6lc-v3-researcher-attempt2/task_summary.json"
)
PROMPT_RULES = """Controlled single-pass acquisition experiment.

You must make exactly one tool call per assistant turn. Never emit multiple tool
calls in one response. Follow this sequence and no other sequence:
1. Call web_search exactly once.
2. Do not call web_search again and do not call database_search.
3. Select the single most relevant and credible SourceRecord from that result.
4. Even if its quality is imperfect, call browser exactly once using only its
   source_record_id.
5. After browser returns an Artifact, call reader exactly once on that artifact.
6. After reader returns, finish immediately without any further tool call.
7. Create an EvidenceCard only when the Reader text genuinely supports it and
   copy every quote verbatim from that Reader text.
8. If the material is insufficient, return cards=[] with a concrete
   no_evidence_reason.
9. Never invent provenance handles or quotes.
"""


class ControlledPromptLibrary:
    """Append experiment-only rules to the formal Researcher system prompt."""

    def __init__(self, delegate) -> None:
        self.delegate = delegate

    def render(self, name: str, **variables: Any) -> RenderedPrompt:
        rendered = self.delegate.render(name, **variables)
        if name != "research/native_tool_loop":
            return rendered
        return RenderedPrompt(
            name=rendered.name,
            version=rendered.version,
            system=f"{rendered.system}\n\n{PROMPT_RULES}",
            user=rendered.user,
        )


def write_json(name: str, value: Any) -> None:
    OUTPUT.mkdir(parents=True, exist_ok=True)
    (OUTPUT / name).write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def successful_observations(trace, name: str):
    return [
        event.observation
        for event in trace
        if event.kind == "tool_result"
        and event.observation is not None
        and event.observation.tool_name == name
        and event.observation.succeeded
    ]


def summarize_task(
    *,
    key: str,
    request: TaskResearchRequest,
    result,
    trace,
    harness_result,
    harness_failure: str | None,
    calls: list[dict[str, Any]],
) -> dict[str, Any]:
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
    attempted_names = [call.name for call in attempted]
    executed_names = [call.name for call in executed]
    errors = [
        event.detail
        for event in trace
        if event.kind == "error" and event.detail
    ]
    multiple = sum(
        len(event.message.tool_calls) > 1
        for event in trace
        if event.kind == "assistant_response" and event.message is not None
    )
    web_observations = successful_observations(trace, "web_search")
    browser_observations = successful_observations(trace, "browser")
    reader_observations = successful_observations(trace, "reader")
    source_record_selected = next(
        (
            call.arguments.get("source_record_id")
            for call in executed
            if call.name == "browser"
        ),
        None,
    )
    artifact_ids = [
        item["artifact_id"]
        for observation in browser_observations
        for item in observation.payload.get("browser_result", {}).get("artifacts", [])
    ]
    reads = [
        observation.payload.get("read_result", {})
        for observation in reader_observations
    ]
    trusted_chars = sum(
        max(0, item.get("char_end", 0) - item.get("char_start", 0))
        for item in reads
        if isinstance(item.get("char_start"), int)
        and isinstance(item.get("char_end"), int)
    )
    finish = None
    finish_error = None
    if harness_result is not None:
        try:
            finish = ResearchFinishDraft.model_validate_json(
                harness_result.final_content or ""
            )
        except Exception as exc:
            finish_error = f"{type(exc).__name__}: {exc}"
    warnings = list(result.warnings)
    builder_error = next(
        (item for item in warnings if "evidence builder failed" in item.lower()),
        None,
    )
    draft_cards = len(finish.cards) if finish else 0
    grounded_cards = len(result.evidence_cards)
    quality = (
        "HIGH"
        if grounded_cards
        else "MEDIUM"
        if trusted_chars >= 1000
        else "LOW"
        if reader_observations
        else "UNUSABLE"
    )
    exact = executed_names == ["web_search", "browser", "reader"] and finish is not None
    return {
        "task_key": key,
        "task_id": request.research_task.task_id,
        "novelty_point_id": request.novelty_point.point_id,
        "language": request.research_task.language,
        "task_type": request.research_task.task_type,
        "status": result.status.value,
        "attempted_tool_sequence": attempted_names,
        "executed_tool_sequence": executed_names,
        "web_search_attempted": attempted_names.count("web_search"),
        "web_search_executed": executed_names.count("web_search"),
        "browser_attempted": attempted_names.count("browser"),
        "browser_executed": executed_names.count("browser"),
        "reader_attempted": attempted_names.count("reader"),
        "reader_executed": executed_names.count("reader"),
        "finish_reached": harness_result is not None,
        "exact_sequence_compliance": exact,
        "multiple_tool_call_count": multiple,
        "protocol_error_count": sum("policy" in item for item in errors),
        "budget_rejection_count": sum("budget" in item for item in errors),
        "source_record_selected": source_record_selected,
        "web_search_success": bool(web_observations),
        "browser_success": bool(browser_observations),
        "artifact_created": bool(artifact_ids),
        "artifact_ids": artifact_ids,
        "artifact_id_available": bool(artifact_ids),
        "reader_success": bool(reader_observations),
        "trusted_read_created": bool(reads),
        "trusted_read_chars": trusted_chars,
        "finish_draft_valid": finish is not None,
        "finish_draft_error": finish_error,
        "draft_card_count": draft_cards,
        "no_evidence_reason": finish.no_evidence_reason if finish else None,
        "model_generated_card": draft_cards > 0,
        "built_evidence_count": len(result.evidence),
        "built_evidence_card_count": grounded_cards,
        "evidence_builder_success": finish is not None and builder_error is None,
        "evidence_builder_error": builder_error,
        "candidate_quality_assessment": quality,
        "ungrounded_quote_failure": bool(
            builder_error and "ungrounded quote" in builder_error.lower()
        ),
        "harness_failure": harness_failure,
        "warnings": warnings,
        "researcher_model_calls": len(calls),
        "researcher_token_usage": calls,
        "researcher_total_tokens": token_total(calls),
    }


async def run() -> dict[str, Any]:
    _load_dev_env()
    config = load_application_config()
    workflow_config = config.project.workflow.model_copy(
        update={"max_rounds": 1, "max_concurrency": 1}
    )
    config = config.model_copy(
        update={
            "project": config.project.model_copy(
                update={"workflow": workflow_config}
            )
        }
    )
    paper = PaperInput.model_validate_json(PAPER_INPUT.read_text(encoding="utf-8"))
    points_payload = json.loads(POINTS_INPUT.read_text(encoding="utf-8"))
    points = [
        NoveltyPoint.model_validate(item)
        for item in points_payload["novelty_points"]
    ]
    built = build_workflow(config)
    researcher = built.services.task_researcher
    researcher.prompts = ControlledPromptLibrary(researcher.prompts)
    measured = MeasuredClient(researcher.model_client, "researcher")
    researcher.model_client = measured
    researcher.harness.model_client = measured
    brief = built.services.coordinator.plan(paper, points=points, attempt=1)
    point_by_id = {point.point_id: point for point in points}

    traces: dict[str, tuple] = {}
    harness_results: dict[str, Any] = {}
    harness_failures: dict[str, str] = {}
    original_harness_run = researcher.harness.run

    async def recording_harness(**kwargs):
        scope = kwargs["scope"]
        key = f"{scope.novelty_point.point_id}/{scope.research_task.task_id}"
        try:
            result = await original_harness_run(**kwargs)
            traces[key] = result.trace
            harness_results[key] = result
            return result
        except Exception as exc:
            traces[key] = tuple(getattr(exc, "trace", ()))
            harness_failures[key] = str(exc)
            raise

    researcher.harness.run = recording_harness
    started_at = datetime.now(timezone.utc).isoformat()
    started = time.perf_counter()
    results = []
    requests = []
    for task in brief.research_tasks:
        request = TaskResearchRequest(
            subject_paper_id=paper.paper_id,
            run_id="v3-attempt2-single-pass-prompt-pretest",
            novelty_point=point_by_id[task.novelty_point_id],
            research_task=task,
            search_plan=built.services.search_planner.plan(
                point_by_id[task.novelty_point_id], task
            ),
        )
        key = f"{task.novelty_point_id}/{task.task_id}"
        token = TASK_KEY.set(key)
        try:
            result = await researcher.ainvoke(request)
        finally:
            TASK_KEY.reset(token)
        requests.append(request)
        results.append(result)
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
    calls_by_task = defaultdict(list)
    for call in measured.calls:
        calls_by_task[call["task_key"]].append(call)
    task_summaries = []
    for request, result in zip(requests, results, strict=True):
        key = f"{request.novelty_point.point_id}/{request.research_task.task_id}"
        task_summaries.append(
            summarize_task(
                key=key,
                request=request,
                result=result,
                trace=traces.get(key, ()),
                harness_result=harness_results.get(key),
                harness_failure=harness_failures.get(key),
                calls=calls_by_task[key],
            )
        )

    exact = sum(item["exact_sequence_compliance"] for item in task_summaries)
    behavior = {
        "task_count": len(task_summaries),
        "exact_sequence_compliance_count": exact,
        "exact_sequence_compliance_rate": exact / len(task_summaries),
        "repeated_search_violation_count": sum(
            item["web_search_attempted"] > 1 for item in task_summaries
        ),
        "browser_skip_count": sum(
            item["browser_executed"] == 0 for item in task_summaries
        ),
        "reader_skip_count": sum(
            item["reader_executed"] == 0 for item in task_summaries
        ),
        "extra_tool_violation_count": sum(
            any(
                name not in {"web_search", "browser", "reader"}
                for name in item["attempted_tool_sequence"]
            )
            for item in task_summaries
        ),
        "multiple_tool_call_count": sum(
            item["multiple_tool_call_count"] for item in task_summaries
        ),
    }
    acquisition = {
        "tasks_with_source_record": sum(
            bool(item["source_record_selected"]) for item in task_summaries
        ),
        "browser_success": sum(item["browser_success"] for item in task_summaries),
        "tasks_with_artifact": sum(item["artifact_created"] for item in task_summaries),
        "reader_success": sum(item["reader_success"] for item in task_summaries),
        "tasks_with_trusted_read": sum(
            item["trusted_read_created"] for item in task_summaries
        ),
        "trusted_read_chars": sum(
            item["trusted_read_chars"] for item in task_summaries
        ),
    }
    finish = {
        "valid_finish_drafts": sum(
            item["finish_draft_valid"] for item in task_summaries
        ),
        "no_evidence_finishes": sum(
            item["finish_draft_valid"] and not item["model_generated_card"]
            for item in task_summaries
        ),
        "draft_cards": sum(item["draft_card_count"] for item in task_summaries),
        "builder_success": sum(
            item["evidence_builder_success"] for item in task_summaries
        ),
        "builder_rejection": sum(
            bool(item["evidence_builder_error"]) for item in task_summaries
        ),
        "grounded_cards": sum(
            item["built_evidence_card_count"] for item in task_summaries
        ),
        "ungrounded_quote_failures": sum(
            item["ungrounded_quote_failure"] for item in task_summaries
        ),
        "evidence": sum(item["built_evidence_count"] for item in task_summaries),
        "evidence_cards": sum(
            item["built_evidence_card_count"] for item in task_summaries
        ),
    }
    original = json.loads(ORIGINAL_TASK_SUMMARY.read_text(encoding="utf-8"))
    original_tokens = sum(item["researcher_total_tokens"] for item in original)
    current_tokens = token_total(measured.calls)
    comparison = {
        "original_attempt2": {
            "web_search_executed": sum(
                item["executed_per_tool_counts"].get("web_search", 0)
                for item in original
            ),
            "browser_executed": 0,
            "reader_executed": 0,
            "trusted_reads": 0,
            "evidence": 0,
            "evidence_cards": 0,
            "researcher_tokens": original_tokens,
        },
        "single_pass_pretest": {
            "web_search_executed": sum(
                item["web_search_executed"] for item in task_summaries
            ),
            "browser_executed": sum(
                item["browser_executed"] for item in task_summaries
            ),
            "reader_executed": sum(
                item["reader_executed"] for item in task_summaries
            ),
            "trusted_reads": acquisition["tasks_with_trusted_read"],
            "evidence": finish["evidence"],
            "evidence_cards": finish["evidence_cards"],
            "researcher_tokens": current_tokens,
        },
        "researcher_token_delta": current_tokens - original_tokens,
        "researcher_token_change_rate": (
            (current_tokens - original_tokens) / original_tokens
            if original_tokens
            else None
        ),
    }
    summary = {
        "behavior": behavior,
        "acquisition": acquisition,
        "finish": finish,
        "tokens": {
            "researcher_total": current_tokens,
            "per_task": {
                item["task_key"]: item["researcher_total_tokens"]
                for item in task_summaries
            },
        },
        "comparison": comparison,
        "levels": {
            "behavior_path": "PASS" if exact >= 4 else "FAIL",
            "acquisition": (
                "PASS"
                if acquisition["tasks_with_artifact"] >= 4
                and acquisition["tasks_with_trusted_read"] >= 4
                else "FAIL"
            ),
            "card_path_proven": "YES" if finish["evidence_cards"] >= 1 else "NO",
        },
        "elapsed_ms": elapsed_ms,
    }
    metadata = {
        "git_sha": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], text=True
        ).strip(),
        "timestamp": started_at,
        "experiment": "V3 Attempt 2 Supplemental Pretest",
        "variant": "Prompt-Controlled Single-Pass Acquisition",
        "input": "persisted MinerU PaperInput and NoveltyPoint checkpoint",
        "task_count": len(task_summaries),
        "workflow_overrides": {"max_rounds": 1, "max_concurrency": 1},
        "prompt_wrapper_target": "research/native_tool_loop",
        "production_prompt_modified": False,
    }
    store = researcher.evidence_builder.reference_store
    write_json("experiment_metadata.json", metadata)
    write_json("effective_config.json", effective_safe_config(config))
    write_json("task_summary.json", task_summaries)
    write_json("experiment_summary.json", summary)
    write_json("token_summary.json", summary["tokens"])
    write_json(
        "manifest_snapshot.json",
        store.load_manifest(paper.paper_id).model_dump(mode="json"),
    )
    write_json(
        "evidence_snapshot.json",
        [item.model_dump(mode="json") for result in results for item in result.evidence],
    )
    write_json(
        "evidence_cards_snapshot.json",
        [
            item.model_dump(mode="json")
            for result in results
            for item in result.evidence_cards
        ],
    )
    write_json(
        "raw_experiment_result.json",
        {"metadata": metadata, "summary": summary, "tasks": task_summaries},
    )
    return {"metadata": metadata, "summary": summary, "tasks": task_summaries}


def main() -> None:
    print(json.dumps(asyncio.run(run()), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
