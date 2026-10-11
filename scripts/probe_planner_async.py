"""Local-model probe of Coordinator -> concurrent SearchPlanner -> fan-in gate.

Runs the production planning nodes, without retrieval or external API calls.
The output directory contains runtime traces and a compact summary.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import time
from pathlib import Path
from urllib.parse import urlparse

from langgraph.graph import END, START, StateGraph

from backend.env import ModelCallOptions, ModelProfile, OpenAICompatibleChatClient, PromptLibrary
from novelty_agent_framework.agents import DemoTaskResearcher, NoveltyCoordinatorAgent, SearchPlannerAgent
from novelty_agent_framework.core import RuntimeArtifactManager, RuntimeDebugConfig
from novelty_agent_framework.schemas import NoveltyPoint, PaperInput
from novelty_agent_framework.workflows import NoveltyWorkflow, NoveltyWorkflowConfig, NoveltyWorkflowServices
from novelty_agent_framework.workflows.state import NoveltyState

REPO = Path(__file__).resolve().parents[1]


async def probe(args):
    if urlparse(args.base_url).hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("This probe only calls a local model endpoint")
    paper = PaperInput.model_validate_json(args.paper.read_text(encoding="utf-8"))
    payload = json.loads(args.points.read_text(encoding="utf-8"))
    points = [NoveltyPoint.model_validate(item) for item in payload["novelty_points"]][:args.limit]
    if len(points) < 2 or args.concurrency < 2:
        raise ValueError("Concurrency verification requires at least two points and slots")
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    client = OpenAICompatibleChatClient(ModelProfile(
        alias="local-async-probe", model=args.model, base_url=args.base_url,
        api_key=os.getenv("LOCAL_VLLM_API_KEY") or "local-probe",
        context_window=32768,
    ))
    planner = SearchPlannerAgent(
        model_client=client, prompts=PromptLibrary(REPO / "backend/src/novelty_agent_framework/prompts"),
        model_options=ModelCallOptions(temperature=0.0, max_tokens=2048,
                                     timeout_seconds=args.timeout, response_format={"type": "json_object"}),
        max_attempts=3,
    )
    config = NoveltyWorkflowConfig(max_rounds=1, max_concurrency=args.concurrency,
        runtime_debug=RuntimeDebugConfig(output_root=output, archive_root=output / "archive"))
    workflow = NoveltyWorkflow(NoveltyWorkflowServices(
        coordinator=NoveltyCoordinatorAgent(research_languages=("en",)),
        search_planner=planner, task_researcher=DemoTaskResearcher(),
    ), config=config, output_root=output)
    manager = RuntimeArtifactManager(paper.paper_id, config=config.runtime_debug)
    intervals = []
    active = 0
    peak = 0
    ticks = 0
    stopped = asyncio.Event()

    async def heartbeat():
        nonlocal ticks
        while not stopped.is_set():
            ticks += 1
            await asyncio.sleep(0.02)

    async def planning(state):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        row = {"point_id": state["current_point"].point_id,
               "task_id": state["current_task"].task_id, "start": time.perf_counter()}
        intervals.append(row)
        try:
            return await workflow._plan_research_task(state)
        finally:
            row["end"] = time.perf_counter()
            active -= 1

    async def gate(state):
        sends = await workflow._dispatch_research_tasks(state)
        if not isinstance(sends, list) or len(sends) != len(points):
            raise AssertionError("Planning gate did not dispatch all tasks")
        return {}

    graph = StateGraph(NoveltyState)
    graph.add_node("plan", workflow._record_stage("plan", workflow._plan))
    graph.add_node("dispatch_planning_tasks", workflow._dispatch_node)
    graph.add_node("plan_research_task", workflow._record_stage("plan_research_task", planning))
    graph.add_node("dispatch_research_tasks", workflow._record_stage("dispatch_research_tasks", gate))
    graph.add_edge(START, "plan")
    graph.add_edge("plan", "dispatch_planning_tasks")
    graph.add_conditional_edges("dispatch_planning_tasks", workflow._dispatch_planning_tasks,
                               ["plan_research_task", "dispatch_research_tasks"])
    graph.add_edge("plan_research_task", "dispatch_research_tasks")
    graph.add_edge("dispatch_research_tasks", END)
    manager.activate()
    started = time.perf_counter()
    ticker = asyncio.create_task(heartbeat())
    summary = {"model": args.model, "base_url": args.base_url,
               "concurrency_limit": args.concurrency, "point_count": len(points)}
    try:
        final = await graph.compile().ainvoke(
            {"paper": paper, "novelty_points": points, "issues": [], "search_plans": [],
             "run_id": manager.run_id}, config={"max_concurrency": args.concurrency})
        plans = final["search_plans"]
        assert len(plans) == len(points)
        assert peak >= 2
        assert ticks > 1
        summary.update(status="SUCCESS", plan_count=len(plans))
        manager.finish_run("SUCCESS")
    except Exception as exc:
        summary.update(status="FAILED", error=f"{type(exc).__name__}: {exc}")
        manager.finish_run("FAILED", error=exc)
        raise
    finally:
        stopped.set()
        await ticker
        summary.update(elapsed_seconds=round(time.perf_counter() - started, 3),
                       peak_planning_tasks=peak, heartbeat_ticks=ticks,
                       task_intervals=[{**row, "start": round(row["start"] - started, 3),
                                        "end": round(row["end"] - started, 3)} for row in intervals],
                       runtime_directory=str(manager.run_dir))
        (output / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        manager.deactivate()
        print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000/v1")
    parser.add_argument("--model", default="qwen2.5-7b-instruct")
    parser.add_argument("--paper", type=Path, default=REPO / "outputs/MF2033k6lC/paper-input/others/paper.json")
    parser.add_argument("--points", type=Path, default=REPO / "outputs/MF2033k6lC/novelty-points.json")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--limit", type=int, default=2)
    parser.add_argument("--concurrency", type=int, default=2)
    parser.add_argument("--timeout", type=float, default=180)
    asyncio.run(probe(parser.parse_args()))


if __name__ == "__main__":
    main()
