"""Run one ResearchTask with a complete Runtime Debug and Gate A lifecycle."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "backend" / "src"))

try:
    from dotenv import load_dotenv

    load_dotenv(PROJECT_ROOT / "backend" / ".env")
except Exception:
    pass

from novelty_agent_framework.config.factory import build_workflow
from novelty_agent_framework.config.loader import load_application_config
from novelty_agent_framework.config.experiment import freeze_config, preflight_config, prepare_startup
from novelty_agent_framework.core.integrity_gates import validate_synthesis_input
from novelty_agent_framework.core.run_identity import file_run_identity
from novelty_agent_framework.core.runtime_artifacts import RuntimeArtifactManager, RuntimeDebugConfig
from novelty_agent_framework.persistence import ReferenceStore
from novelty_agent_framework.schemas import (
    NoveltyPoint,
    PaperInput,
    ResearchTask,
    SearchPlan,
    TaskResearchRequest,
    TaskResearchStatus,
)


def new_run_id(task_id: str) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    safe_task = "".join(char if char.isalnum() or char in "-_" else "-" for char in task_id)
    return f"single-{safe_task}-{stamp}-{uuid.uuid4().hex[:8]}"


def build_request(
    output_root: Path,
    paper_id: str,
    *,
    run_id: str,
    point_id: str,
    task_id: str,
    source_contents: dict[str, bytes] | None = None,
) -> TaskResearchRequest:
    contents = source_contents if source_contents is not None else load_task_sources(output_root, paper_id)
    raw_points = json.loads(contents["novelty-points.json"])["novelty_points"]
    matched_points = [item for item in raw_points if item.get("point_id") == point_id]
    if len(matched_points) != 1:
        raise SystemExit(
            f"NoveltyPoint 必须精确匹配一次：point_id={point_id!r}; "
            f"matched={len(matched_points)}"
        )
    point = NoveltyPoint.model_validate(matched_points[0])
    plans = json.loads(contents["retrieval-plans.json"])["novelty_point_plans"]
    matched_bundles = [item for item in plans if item.get("novelty_point_id") == point_id]
    if len(matched_bundles) != 1:
        raise SystemExit(
            f"Retrieval plan bundle 必须精确匹配一次：point_id={point_id!r}; "
            f"matched={len(matched_bundles)}"
        )
    bundle = matched_bundles[0]
    matched_tasks = [
        item for item in bundle.get("research_tasks", []) if item.get("task_id") == task_id
    ]
    matched_plans = [
        item for item in bundle.get("search_plans", []) if item.get("task_id") == task_id
    ]
    if len(matched_tasks) != 1 or len(matched_plans) != 1:
        raise SystemExit(
            f"ResearchTask/SearchPlan 必须按 ID 各精确匹配一次："
            f"point_id={point_id!r}, task_id={task_id!r}; "
            f"tasks={len(matched_tasks)}, plans={len(matched_plans)}"
        )
    task = ResearchTask.model_validate(matched_tasks[0])
    if task.novelty_point_id != point_id:
        raise SystemExit(f"ResearchTask {task_id!r} 未绑定 NoveltyPoint {point_id!r}")
    return TaskResearchRequest(
        subject_paper_id=paper_id,
        run_id=run_id,
        novelty_point=point,
        research_task=task,
        search_plan=SearchPlan.model_validate(matched_plans[0]),
    )


def load_task_sources(output_root: Path, paper_id: str) -> dict[str, bytes]:
    """Capture precisely the source bytes consumed when constructing a request."""
    return {name: (output_root / paper_id / name).read_bytes()
            for name in ("novelty-points.json", "retrieval-plans.json")}


def task_input_provenance(request: TaskResearchRequest, output_root: Path,
                          source_contents: dict[str, bytes]) -> dict:
    payload = request.model_dump(mode="json")

    def digest(value):
        return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                         separators=(",", ":")).encode()).hexdigest()

    return {
        "task_request_sha256": digest(payload),
        "task_input_sha256": digest({key: value for key, value in payload.items() if key != "run_id"}),
        "task_source_files": {
            name: {"path": str((output_root / request.subject_paper_id / name).resolve()),
                   "sha256": hashlib.sha256(content).hexdigest()}
            for name, content in source_contents.items()
        },
    }


def _project_path(value: str | Path) -> Path:
    path = Path(value)
    return (path if path.is_absolute() else PROJECT_ROOT / path).resolve()


def terminal_result(task_status: TaskResearchStatus, gate_rejected: int) -> tuple[str, int]:
    if task_status == TaskResearchStatus.FAILED:
        return "SUCCESS", 1
    if task_status == TaskResearchStatus.PARTIAL or gate_rejected:
        return "SUCCESS", 2
    return "SUCCESS", 0


async def main(args: argparse.Namespace) -> int:
    config = load_application_config(profile_path=getattr(args, "profile", None),
                                     environ={} if getattr(args, "isolated_config", False) else None)
    issues = preflight_config(config, require_reviewer=False, active_roles=("researcher",))
    if any(issue["severity"] == "error" for issue in issues):
        raise ValueError("configuration preflight failed: " + json.dumps(issues))
    settings = config.project.runtime_debug
    output_root = _project_path(settings.output_root)
    # This diagnostic entrypoint requires recording; freeze the override too.
    settings.enabled = True
    settings.output_root = str(output_root)
    settings.archive_root = str(_project_path(settings.archive_root))
    settings.llm_pricing_path = str(_project_path(
        settings.llm_pricing_path or RuntimeDebugConfig().llm_pricing_path
    ))
    runtime = RuntimeDebugConfig(
        **{**settings.model_dump(mode="python"),
           "output_root": output_root, "archive_root": Path(settings.archive_root),
           "llm_pricing_path": Path(settings.llm_pricing_path)}
    )
    paper_json = Path(args.paper_json).resolve(strict=True)
    paper = PaperInput.model_validate_json(paper_json.read_text(encoding="utf-8"))
    paper_id = paper.paper_id
    run_id = new_run_id(args.task_id)
    source_contents = load_task_sources(output_root, paper_id)
    request = build_request(
        output_root,
        paper_id,
        run_id=run_id,
        point_id=args.point_id,
        task_id=args.task_id,
        source_contents=source_contents,
    )
    label = f"{request.novelty_point.point_id}/{request.research_task.task_id}"
    print(f"论文 : {paper_id}")
    print(f"任务 : {label}  language={request.research_task.language}")
    print(f"Run  : {run_id}")

    frozen = prepare_startup(config, entrypoint="single_task", input_path=paper_json,
                             output_root=output_root,
                             snapshot_dir=output_root / ".startup" / run_id,
                             input_contents={**source_contents, "task-request.json": request.model_dump_json().encode()},
                             active_roles=("researcher",), require_reviewer=False)
    workflow = build_workflow(config, output_root=output_root)
    frozen["input"].update(task_input_provenance(request, output_root, source_contents))
    researcher = workflow.services.task_researcher
    manager = RuntimeArtifactManager(
        paper_id,
        config=runtime,
        run_id=run_id,
        run_identity=file_run_identity(
            "single_task",
            paper_json,
            project_root=PROJECT_ROOT,
            novelty_point_id=request.novelty_point.point_id,
            task_id=request.research_task.task_id,
            search_plan_id=request.search_plan.task_id,
        ),
        runtime_config={
            **frozen["effective_config"],
            "configuration_manifest": {key: value for key, value in frozen.items() if key != "effective_config"},
            "script_mode": "single_research_task_diagnostics",
            "point_id": request.novelty_point.point_id,
            "task_id": request.research_task.task_id,
            "language": request.research_task.language,
            "enabled_tools": list(researcher.tools.names),
        },
        enabled_tools=list(researcher.tools.names),
        stage_names=["run_research_task", "validate_synthesis_input"],
    )
    manager.activate()
    started = time.perf_counter()
    result = None
    gate = None
    terminal_status = "FAILED"
    exit_code = 1
    terminal_error: BaseException | None = None
    try:
        research_stage = manager.start_stage("run_research_task", request)
        try:
            result = await researcher.ainvoke(request)
        except BaseException as exc:
            manager.fail_stage(research_stage, exc)
            raise
        manager.finish_stage(research_stage, result)

        gate_stage = manager.start_stage(
            "validate_synthesis_input",
            {
                "evidence_cards": result.evidence_cards,
                "evidence": result.evidence,
                "tasks": [request.research_task],
                "novelty_points": [request.novelty_point],
            },
        )
        try:
            gate = validate_synthesis_input(
                result.evidence_cards,
                evidence=result.evidence,
                tasks=[request.research_task],
                novelty_points=[request.novelty_point],
                paper_id=paper_id,
                reference_store=ReferenceStore(output_root),
            )
        except BaseException as exc:
            manager.fail_stage(gate_stage, exc)
            raise
        gate_audit = gate.audit()
        manager.finish_stage(gate_stage, {"synthesis_integrity": gate_audit})
        terminal_status, exit_code = terminal_result(result.status, len(gate.rejected))
        manager.record_outcome(
            {
                "point_id": request.novelty_point.point_id,
                "task_id": request.research_task.task_id,
                "task_status": result.status.value,
                "steps_used": result.steps_used,
                "read_count": len(result.read_results),
                "evidence_count": len(result.evidence),
                "card_count_before_gate_a": len(result.evidence_cards),
                "card_count_after_gate_a": len(gate.accepted),
                "gate_a_validation_passed": gate_audit["validation_passed"],
                "gate_a_rejected_card_ids": gate_audit["rejected_card_ids"],
                "warning_count": len(result.warnings),
            }
        )
    except (KeyboardInterrupt, asyncio.CancelledError) as exc:
        terminal_status, exit_code, terminal_error = "INTERRUPTED", 130, exc
    except BaseException as exc:
        terminal_status, exit_code, terminal_error = "FAILED", 1, exc
        print(f"运行失败: {type(exc).__name__}: {exc}", file=sys.stderr)
    finally:
        try:
            manager.finish_run(terminal_status, error=terminal_error)
        finally:
            manager.deactivate()

    print(f"\n耗时 {time.perf_counter() - started:.1f}s  run 目录: {manager.run_dir}")
    if result is not None:
        print(f"状态      : {result.status.value}")
        print(f"读取次数  : {len(result.read_results)}")
        print(f"证据卡    : {len(result.evidence_cards)}")
        print(f"证据条数  : {len(result.evidence)}")
        print(f"步数      : {result.steps_used}")
    if gate is not None:
        print(
            f"Gate A    : 通过 {len(gate.accepted)} / 拒绝 {len(gate.rejected)}  "
            f"validation_passed={gate.audit()['validation_passed']}"
        )
    print(f"Runtime   : {terminal_status}; exit={exit_code}")
    return exit_code


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--paper-json", required=True, type=Path)
    parser.add_argument("--profile", type=Path)
    parser.add_argument("--isolated-config", action="store_true")
    parser.add_argument("--point-id", required=True)
    parser.add_argument("--task-id", required=True)
    raise SystemExit(asyncio.run(main(parser.parse_args())))
