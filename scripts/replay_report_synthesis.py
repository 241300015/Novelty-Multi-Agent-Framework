"""Replay only the frozen report nodes; never launch upstream research."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import shutil
import signal
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

from backend.env import ModelResponse, reset_model_call_budget, set_model_call_budget
from novelty_agent_framework.agents import NoveltyCoordinatorAgent
from novelty_agent_framework.config import build_workflow
from novelty_agent_framework.core.report_binding import assemble_report_from_draft
from novelty_agent_framework.persistence import persist_workflow_input, persist_novelty_reviews
from novelty_agent_framework.report_inputs import record_report_input_files
from novelty_agent_framework.schemas.failures import FailureEvent, RecoveryDecision
from novelty_agent_framework.core.runtime_artifacts import RuntimeArtifactManager, RuntimeDebugConfig
from novelty_agent_framework.schemas import (
    Evidence, EvidenceCard, InsufficientFinalEvidence, NoveltyBrief, NoveltyPoint,
    NoveltyPointReview, PaperInput, RejectedEvidence, ReportNarrativeDraft,
    ResearchTask, SearchPlan, TaskResearchResult, WorkflowIssue,
)
from novelty_agent_framework.services.model_budget import RunModelBudget


class OfflineDraftClient:
    """A declared test substitute; it never delegates to an external client."""

    def __init__(self):
        self.calls = 0

    def complete(self, messages, *, options=None):
        self.calls += 1
        return ModelResponse(content=json.dumps({
            "summary": "离线脚本模型测试文本；此处没有重新评价原 Reviewer 结论。",
        }, ensure_ascii=False))



class CapturedDraftClient:
    """Replay a stored real response without any external model call."""

    def __init__(self, response_file: Path):
        record = json.loads(response_file.read_text())
        if record.get("status") != "SUCCESS" or not record.get("response", {}).get("content"):
            raise ValueError(f"captured response is unavailable: {response_file}")
        self.content = record["response"]["content"]
        self.finish_reason = record.get("finish_reason")
        self.calls = 0

    def complete(self, messages, *, options=None):
        self.calls += 1
        return ModelResponse(content=self.content,
                             raw={"choices": [{"finish_reason": self.finish_reason}]})


class CapturedReportCoordinator:
    """Historical whole-report responses are assembled, never sent to a new LLM contract."""

    def __init__(self, fixture):
        self.fixture = fixture

    def synthesize(self, paper, *, brief, evidence, novelty_reviews, rejected_evidence,
                   insufficient_final_evidence_points):
        response = self.fixture.complete([])
        if self.fixture.finish_reason != "stop":
            raise ValueError("captured report response did not finish normally")
        return assemble_report_from_draft(ReportNarrativeDraft.model_validate_json(response.content),
            paper_id=paper.paper_id, novelty_points=brief.novelty_points,
            evidence_cards=evidence, novelty_reviews=novelty_reviews, rejected_evidence=rejected_evidence)


class CapturedPointCoordinator(NoveltyCoordinatorAgent):
    """Replay a completed point batch only against exactly the same point inputs."""

    def __init__(self, path):
        super().__init__()
        batch = json.loads(path.read_text())
        if (batch.get("protocol") != "point_synthesis_v1" or batch.get("status") != "completed"
                or batch.get("barrier_complete") is not True):
            raise ValueError("captured point batch is incomplete")
        self.materials = {row["novelty_point"]["point_id"]: row for row in batch["point_inputs"]}
        self.results = {row["point_id"]: row for row in batch["point_results"]}
        order = batch["point_order"]
        if (len(set(order)) != len(order) or set(order) != set(self.materials)
                or set(order) != set(self.results) or len(batch["point_inputs"]) != len(order)
                or len(batch["point_results"]) != len(order)
                or any(row["status"] != "completed" for row in self.results.values())):
            raise ValueError("captured point batch coverage is invalid")
        self.paper_id, self.calls = batch["paper_id"], 0

    async def asynthesize(self, paper, *, brief, **kwargs):
        if paper.paper_id != self.paper_id or set(p.point_id for p in brief.novelty_points) != set(self.materials):
            raise ValueError("captured point batch scope differs")
        return await super().asynthesize(paper, brief=brief, **kwargs)

    async def synthesize_point(self, materials, *, schema, repair_feedback=None):
        point_id = materials["novelty_point"]["point_id"]
        if repair_feedback is not None or materials != self.materials.get(point_id):
            raise ValueError("captured point input differs or needs new generation")
        self.calls += 1
        return ModelResponse(content=json.dumps({"summary": self.results[point_id]["summary"]}))


def _check_frozen_gate(raw):
    gate = raw.get("synthesis_integrity") or {}
    if gate.get("validation_passed") is not True:
        raise ValueError("frozen upstream gate did not pass")
    if "accepted_card_ids" in gate:  # Historical Gate A checked Cards.
        if set(gate["accepted_card_ids"]) != {c["card_id"] for c in raw.get("evidence_cards", [])}:
            raise ValueError("frozen upstream evidence gate does not match accepted cards")
    elif (gate.get("output_review_count") != len(raw.get("novelty_reviews", []))
          or gate.get("rejected_review_count") != 0):
        raise ValueError("frozen upstream review gate does not match Reviews")


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _typed_state(raw: dict) -> dict:
    state = dict(raw)
    state["paper"] = PaperInput.model_validate(raw["paper"])
    state["brief"] = NoveltyBrief.model_validate(raw["brief"])
    for key, model in (
        ("novelty_points", NoveltyPoint),
        ("evidence_cards", EvidenceCard),
        ("raw_evidence", Evidence),
        ("all_research_tasks", ResearchTask),
        ("search_plans", SearchPlan),
        ("task_research_results", TaskResearchResult),
        ("issues", WorkflowIssue),
        ("failure_events", FailureEvent),
        ("recovery_decisions", RecoveryDecision),
        ("recovery_history", RecoveryDecision),
        ("novelty_reviews", NoveltyPointReview),
        ("rejected_evidence", RejectedEvidence),
        ("insufficient_final_evidence_points", InsufficientFinalEvidence),
    ):
        state[key] = [model.model_validate(item) for item in raw.get(key, [])]
    return state


def _copy_renderer_inputs(source_input: Path, source_run_id: str, output_root: Path, paper_id: str) -> None:
    source_workspace = source_input.parents[5] / source_run_id
    target = output_root / paper_id
    for relative in (
        "paper-input/others/paper.json", "novelty-points.json", "retrieval-plans.json",
        "evidence-cards.json", "references/list.json", "candidate-audit.json",
    ):
        source = source_workspace / relative
        if source.is_file():
            destination = target / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
    required = ("paper-input/others/paper.json", "novelty-points.json",
                "retrieval-plans.json", "evidence-cards.json")
    missing = [name for name in required if not (target / name).is_file()]
    if missing:
        raise FileNotFoundError(f"Renderer source artifacts missing: {missing}")


def _timeout_handler(signum, frame):
    raise TimeoutError("report_node_deadline_exceeded")


async def _one(row: dict, mode: str, deadline_seconds: int, captured_tag: str | None = None) -> dict:
    source = Path(row["synthesize_stage_input"])
    if _sha(source) != row["synthesize_stage_input_sha256"]:
        raise ValueError(f"source input hash mismatch: {row['label']}")
    raw = json.loads(source.read_text())
    _check_frozen_gate(raw)
    state = _typed_state(raw)
    if state["paper"].paper_id != row["paper_id"]:
        raise ValueError("source paper_id mismatch")
    output_root = Path(row["recovery_output_root"])
    if output_root.exists():
        raise FileExistsError(f"recovery output already exists: {output_root}")
    output_root.mkdir(parents=True)
    _copy_renderer_inputs(source, row["source_run_id"], output_root, row["paper_id"])
    state["run_id"] = row["recovery_id"]
    persist_workflow_input(state["paper"], output_root=output_root, run_id=state["run_id"])
    # Register only the explicitly copied frozen inputs under the recovery run.
    workspace = output_root / row["paper_id"]
    record_report_input_files(workspace, tuple(name for name in (
        "novelty-points.json", "retrieval-plans.json", "evidence-cards.json",
        "candidate-audit.json", "references/list.json") if (workspace / name).is_file()), run_id=state["run_id"])
    persist_novelty_reviews(state["paper"], state["novelty_reviews"], output_root=output_root,
        integrity=raw.get("synthesis_integrity"), card_reviews=raw.get("review_card_results", []),
        run_id=state["run_id"])
    workflow = build_workflow(output_root=output_root)
    if mode in {"offline", "captured"}:
        if mode == "offline":
            fixture = OfflineDraftClient()
        else:
            source_root = Path(row["recovery_output_root"].rsplit("-captured-reassembly", 1)[0]
                               + "-" + str(captured_tag))
            source_id = row["recovery_id"].rsplit("-captured-reassembly", 1)[0] + "-" + str(captured_tag)
            batch_path = (source_root / row["paper_id"] / "report-synthesis"
                          / (hashlib.sha256(source_id.encode()).hexdigest()[:24] + ".json"))
            if batch_path.exists():
                fixture = CapturedPointCoordinator(batch_path)
            else:
                # Explicit legacy archive layout, used by captured-response publication.
                response_file = (source_root / row["paper_id"] / "runtime" / source_id
                                 / "llm_calls" / "0001_deepseek-flash.json")
                fixture = CapturedDraftClient(response_file)
        configured = workflow.services.coordinator
        coordinator = (fixture if isinstance(fixture, CapturedPointCoordinator) else
            CapturedReportCoordinator(fixture) if mode == "captured" else
            NoveltyCoordinatorAgent(model_client=fixture, prompts=configured._prompts,
                                    model_options=configured.model_options))
        workflow.services = replace(workflow.services, coordinator=coordinator)
    else:
        fixture = None
    manager = RuntimeArtifactManager(
        row["paper_id"], run_id=row["recovery_id"],
        run_identity={"mode": "report_node_recovery", "source_run_id": row["source_run_id"],
                      "recovery_id": row["recovery_id"], "upstream_reused": True,
                      "upstream_recomputed": False},
        config=RuntimeDebugConfig(output_root=output_root,
                                  archive_root=output_root / "runtime-archive",
                                  max_model_calls=max(4, 3 * len(state["novelty_points"]))),
        stage_names=("synthesize_report", "validate_report_integrity",
                     "persist_report", "render_report"),
    )
    result = {"mode": f"report_node_recovery_{mode}", "source_run_id": row["source_run_id"],
              "source_run_status": "FAILED", "recovery_id": row["recovery_id"],
              "paper_id": row["paper_id"], "upstream_reused": True,
              "upstream_recomputed": False, "recomputed_stages": [], "status": "RUNNING"}
    manager.activate()
    previous_handler = signal.signal(signal.SIGALRM, _timeout_handler)
    signal.alarm(deadline_seconds)
    try:
        for name, method in (
            ("synthesize_report", workflow._synthesize_report),
            ("validate_report_integrity", workflow._validate_report_integrity),
            ("persist_report", workflow._persist_report),
            ("render_report", workflow._render_report),
        ):
            update = await workflow._record_stage(name, method)(state)
            state.update(update)
            result["recomputed_stages"].append(name)
            if name == "validate_report_integrity" and not update["report_integrity"]["validation_passed"]:
                raise ValueError("report_integrity_failed: " + repr(update["report_integrity"]["issues"]))
        report_json = output_root / row["paper_id"] / "report.json"
        report_md = Path(state["rendered_report_path"])
        result.update(status="SUCCESS", report_json=str(report_json), report_json_sha256=_sha(report_json),
                      report_markdown=str(report_md), report_markdown_sha256=_sha(report_md),
                      point_ids=[item.novelty_point_id for item in state["report"].conclusions],
                      local_response_replay_calls=fixture.calls if fixture else None)
        manager.finish_run("SUCCESS")
    except BaseException as exc:
        result.update(status="FAILED", error_type=type(exc).__name__, error=str(exc)[:1000])
        manager.finish_run("FAILED", error=exc)
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous_handler)
        manager.deactivate()
        (output_root / "recovery-result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-manifest", required=True, type=Path)
    parser.add_argument("--mode", choices=("offline", "captured", "live"), required=True)
    parser.add_argument("--sources", nargs="+", default=["L1", "L2"])
    parser.add_argument("--budget-rmb", type=Decimal)
    parser.add_argument("--max-attempts", type=int)
    parser.add_argument("--deadline-seconds", type=int, default=240)
    parser.add_argument("--offline-tag", default="offline", help="append-only local output suffix")
    parser.add_argument("--resume-live", action="store_true", help="reuse an existing total budget ledger")
    parser.add_argument("--live-tag", help="new append-only output identity when resuming live")
    parser.add_argument("--captured-tag", help="source live output suffix for zero-call reassembly")
    args = parser.parse_args()
    if args.mode == "live" and (args.budget_rmb is None or args.max_attempts is None):
        parser.error("live mode requires an explicitly approved total budget and attempt limit")
    if args.resume_live and (args.mode != "live" or not args.live_tag):
        parser.error("--resume-live requires live mode and a new --live-tag")
    if args.mode == "captured" and not args.captured_tag:
        parser.error("captured mode requires --captured-tag")
    manifest = json.loads(args.source_manifest.read_text())
    rows = {row["label"]: dict(row) for row in manifest["sources"]}
    for label in args.sources:
        if label not in rows:
            parser.error(f"unknown source {label}")
        if args.mode == "offline":
            rows[label]["recovery_output_root"] += "-" + args.offline_tag
        elif args.mode == "captured":
            rows[label]["recovery_output_root"] += "-captured-reassembly"
            rows[label]["recovery_id"] += "-captured-reassembly"
        elif args.resume_live:
            rows[label]["recovery_output_root"] += "-" + args.live_tag
            rows[label]["recovery_id"] += "-" + args.live_tag
    token = None
    if args.mode == "live":
        ledger_path = Path("outputs/report-node-recovery-20260918/model-budget-ledger.json")
        if ledger_path.exists() and not args.resume_live:
            parser.error(f"live budget ledger already exists; refusing a reset: {ledger_path}")
        budget = RunModelBudget(ledger_path,
                                cap_rmb=args.budget_rmb, max_attempts=args.max_attempts,
                                resume=args.resume_live)
        token = set_model_call_budget(budget)
    try:
        for label in args.sources:
            result = asyncio.run(_one(rows[label], args.mode, args.deadline_seconds,
                                      captured_tag=args.captured_tag))
            print(json.dumps(result, ensure_ascii=False))
            if result["status"] != "SUCCESS":
                break
    finally:
        if token is not None:
            reset_model_call_budget(token)


if __name__ == "__main__":
    main()
