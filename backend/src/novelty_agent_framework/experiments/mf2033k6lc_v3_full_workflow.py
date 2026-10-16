"""MF2033k6lC V3: resume the formal workflow from verified MinerU checkpoints."""

from __future__ import annotations

import asyncio
import contextvars
import json
import time
from collections import defaultdict
from pathlib import Path

from backend.env.model_client import _load_dev_env

from ..config.factory import build_workflow, load_config
from ..schemas import NoveltyPoint, PaperInput, TaskResearchResult
from ..workflows import NoveltyWorkflow, NoveltyWorkflowConfig, NoveltyWorkflowServices
from .evidence_card_builder_live_smoke import MeasuredModelClient, _trace

PDF = Path("examples/MF2033k6lC.pdf")
OUTPUT = Path("outputs/experiments/mf2033k6lc-v3-full-workflow")
BACKUP = Path("backups/MF2033k6lC/v2")
TASK_ID = contextvars.ContextVar("mf2033_task_id", default="unknown")
PAPER_INPUT = Path("outputs/MF2033k6lC/paper-input/others/paper.json")
POINTS_INPUT = Path("outputs/MF2033k6lC/novelty-points.json")
CONTENT_LIST = Path("outputs/MF2033k6lC/paper-input/content-list.json")



class PersistedPointExtractor:
    """Resume after the already-completed real point-extraction checkpoint."""

    def __init__(self, points):
        self.points = tuple(points)

    def extract(self, digest, *, previous_brief, attempt):
        return self.points


class RecordingResearcher:
    def __init__(self, delegate):
        self.delegate = delegate
        self.requests = []
        self.results: list[TaskResearchResult] = []
        self.results_by_key: dict[str, TaskResearchResult] = {}
        self.elapsed_ms = {}

    async def ainvoke(self, request):
        task_key = f"{request.novelty_point.point_id}/{request.research_task.task_id}"
        token = TASK_ID.set(task_key)
        self.requests.append(request)
        started = time.perf_counter()
        try:
            result = await self.delegate.ainvoke(request)
            self.results.append(result)
            self.results_by_key[task_key] = result
            return result
        finally:
            self.elapsed_ms[task_key] = round(
                (time.perf_counter() - started) * 1000, 3)
            TASK_ID.reset(token)


def write_json(name, value):
    OUTPUT.mkdir(parents=True, exist_ok=True)
    (OUTPUT / name).write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


async def run():
    _load_dev_env()
    raw = load_config()
    raw["workflow"].update({"max_rounds": 1, "max_concurrency": 1,
                            "min_final_evidence_cards_per_point": 1})
    raw["task_researcher"].update({"max_steps": 12, "max_tool_calls": 10})
    processing = raw["processing"]
    paper = PaperInput.model_validate(json.loads(PAPER_INPUT.read_text(encoding="utf-8")))
    points_payload = json.loads(POINTS_INPUT.read_text(encoding="utf-8"))
    points = [NoveltyPoint.model_validate(item) for item in points_payload["novelty_points"]]
    content_list = json.loads(CONTENT_LIST.read_text(encoding="utf-8"))
    if paper.metadata.get("source") != "mineru":
        raise RuntimeError("persisted PaperInput is not a verified MinerU artifact")

    built = build_workflow(raw)
    task_researcher = built.services.task_researcher
    research_client = task_researcher.model_client
    measured = MeasuredModelClient(research_client, research_client.profile.model)
    original_complete = measured.acomplete
    async def tagged_complete(messages, *, options=None):
        before = len(measured.calls)
        response = await original_complete(messages, options=options)
        measured.calls[before]["task_id"] = TASK_ID.get()
        return response
    measured.acomplete = tagged_complete
    task_researcher.model_client = measured
    task_researcher.harness.model_client = measured

    traces = {}
    harness_failures = {}
    original_harness_run = task_researcher.harness.run
    async def recording_harness(**kwargs):
        scope = kwargs["scope"]
        task_id = f"{scope.novelty_point.point_id}/{scope.research_task.task_id}"
        try:
            result = await original_harness_run(**kwargs)
            traces[task_id] = result.trace
            return result
        except Exception as exc:
            traces[task_id] = tuple(getattr(exc, "trace", ()))
            harness_failures[task_id] = str(exc)
            raise
    task_researcher.harness.run = recording_harness

    builder_calls = []
    original_build = task_researcher.evidence_builder.build
    def recording_build(*args, **kwargs):
        result = original_build(*args, **kwargs)
        scope = kwargs["scope"]
        builder_calls.append({"task_key": f"{scope.novelty_point.point_id}/{scope.research_task.task_id}",
            "evidence_count": len(result.evidence),
            "evidence_card_count": len(result.evidence_cards),
            "warnings": list(result.warnings)})
        return result
    task_researcher.evidence_builder.build = recording_build

    recording = RecordingResearcher(task_researcher)
    workflow = NoveltyWorkflow(NoveltyWorkflowServices(
        coordinator=built.services.coordinator,
        task_researcher=recording,
        search_planner=built.services.search_planner,
        point_extractor=PersistedPointExtractor(points)),
        NoveltyWorkflowConfig(max_rounds=1, max_concurrency=1,
                              min_final_evidence_cards_per_point=1))
    workflow_started = time.perf_counter()
    workflow_result = await workflow.arun(paper)
    workflow_ms = round((time.perf_counter() - workflow_started) * 1000, 3)

    trace_dir = OUTPUT / "traces-coordinator-rerun"
    trace_dir.mkdir(parents=True, exist_ok=True)
    for task_id, trace in traces.items():
        trace_path = trace_dir / f"{task_id}.jsonl"
        trace_path.parent.mkdir(parents=True, exist_ok=True)
        trace_path.write_text("".join(
            json.dumps(_trace(i, event), ensure_ascii=False) + "\n"
            for i, event in enumerate(trace, 1)), encoding="utf-8")

    calls_by_task = defaultdict(list)
    for call in measured.calls:
        calls_by_task[call.get("task_id", "unknown")].append(call)
    result_by_task = recording.results_by_key
    task_summaries = []
    multi_total = 0
    for request in recording.requests:
        task_id = request.research_task.task_id
        task_key = f"{request.novelty_point.point_id}/{task_id}"
        trace = traces.get(task_key, ())
        tools = [event.tool_call.name for event in trace if event.kind == "tool_call"]
        rejection_count = sum(
            event.kind == "error" and event.detail and "multiple tool calls" in event.detail
            for event in trace)
        multi_total += rejection_count
        result = result_by_task.get(task_key)
        usages = [call["usage"] for call in calls_by_task[task_key]]
        task_summaries.append({
            **request.research_task.model_dump(mode="json"),
            "task_key": task_key,
            "status": result.status.value if result else "missing",
            "steps_used": result.steps_used if result else 0,
            "tool_sequence": tools,
            "web_search_count": tools.count("web_search"),
            "browser_count": tools.count("browser"),
            "reader_count": tools.count("reader"),
            "finish_success": bool(result and result.status.value == "completed"),
            "trusted_read_count": len(result.read_results) if result else 0,
            "evidence_count": len(result.evidence) if result else 0,
            "evidence_card_count": len(result.evidence_cards) if result else 0,
            "warnings": list(result.warnings) if result else [harness_failures.get(task_key, "missing result")],
            "elapsed_ms": recording.elapsed_ms.get(task_key),
            "token_usage": usages,
            "total_tokens": sum(x.get("total_tokens", 0) for x in usages),
            "multi_tool_call_rejection_count": rejection_count,
        })

    all_evidence = [ev for result in recording.results for ev in result.evidence]
    all_cards = [card for result in recording.results for card in result.evidence_cards]
    backup_count = sum(1 for path in BACKUP.rglob("*") if path.is_file())
    backup_bytes = sum(path.stat().st_size for path in BACKUP.rglob("*") if path.is_file())
    processing_summary = {
        "paper_id": paper.paper_id, "pdf_path": str(PDF),
        "actual_parser": paper.metadata["source"], "mineru_version": "3.4.5",
        "backend": processing["mineru_backend"], "method": processing["mineru_method"],
        "page_count": content_list["page_count"], "full_text_chars": len(paper.full_text),
        "reference_count": len(paper.references), "image_count": len(paper.images),
        "table_count": len(paper.tables), "equation_count": len(paper.equations),
        "parse_warning_count": len(content_list.get("parse_warnings", [])),
        "parse_warnings": content_list.get("parse_warnings", []), "title": paper.title,
        "abstract_chars": len(paper.abstract),
        "keywords_zh_count": len(paper.keywords_zh),
        "keywords_en_count": len(paper.keywords_en),
        "processing_elapsed_ms": None,
        "processing_elapsed_data_gap": "first full MinerU run completed before the failed workflow attempt, but elapsed time was not persisted",
        "resume_mode": "persisted_real_mineru_paper_and_points",
        "mineru_reexecuted": False,
        "paper_input_counts": {"images": len(paper.images), "tables": len(paper.tables),
                               "equations": len(paper.equations)},
    }
    workflow_summary = {
        "novelty_point_count": len(workflow_result.brief.novelty_points),
        "research_task_count": len(workflow_result.brief.research_tasks),
        "dispatched_task_count": len(recording.requests),
        "completed_task_count": sum(x.status.value == "completed" for x in recording.results),
        "partial_task_count": sum(x.status.value == "partial" for x in recording.results),
        "failed_task_count": sum(x.status.value == "failed" for x in recording.results),
        "empty_evidence_task_count": sum(not x.evidence for x in recording.results),
        "evidence_count": len(all_evidence), "evidence_card_count": len(all_cards),
        "multi_tool_call_rejection_count_total": multi_total,
        "workflow_elapsed_ms": workflow_ms, "report_generated": True,
        "validator_mode": "experimental_passthrough",
        "production_validator_migrated": False,
        "reviewer_enabled": False, "reviewer_migrated": False,
        "fan_in_complete": len(recording.results) == len(recording.requests),
        "task_key_scheme": "novelty_point_id/task_id",
    }
    token_summary = {"research_model_calls": len(measured.calls),
        "research_total_tokens": sum(call["usage"].get("total_tokens", 0) for call in measured.calls),
        "per_task": {item["task_key"]: item["total_tokens"] for item in task_summaries},
        "task_key_scheme": "novelty_point_id/task_id",
        "planning_tokens_not_instrumented": True}
    store = task_researcher.evidence_builder.reference_store
    write_json("processing_summary.json", processing_summary)
    write_json("workflow_summary.json", workflow_summary)
    write_json("task_summary.json", task_summaries)
    write_json("token_summary.json", token_summary)
    write_json("manifest_snapshot.json", store.load_manifest("MF2033k6lC").model_dump(mode="json"))
    write_json("evidence_snapshot.json", [x.model_dump(mode="json") for x in all_evidence])
    write_json("evidence_cards_snapshot.json", [x.model_dump(mode="json") for x in all_cards])
    write_json("workflow_result.json", workflow_result.model_dump(mode="json"))
    write_json("v3_raw_summary.json", {"processing": processing_summary,
        "workflow": workflow_summary, "tokens": token_summary,
        "prior_attempts": [{"attempt": 1, "status": "failed",
            "failure_layer": "synthesize_report",
            "reason": "configured GLM-4.7 endpoint returned 403 Model disabled"}],
        "coordinator_model": raw["agents"]["coordinator"]["model"],
        "resume_checkpoint": "coordinator (persisted real MinerU PaperInput and point-extraction output)",
        "backup_after": {"file_count": backup_count, "total_bytes": backup_bytes},
        "builder_calls": builder_calls})
    return {"processing": processing_summary, "workflow": workflow_summary,
            "tokens": token_summary, "tasks": task_summaries}


def main():
    print(json.dumps(asyncio.run(run()), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
