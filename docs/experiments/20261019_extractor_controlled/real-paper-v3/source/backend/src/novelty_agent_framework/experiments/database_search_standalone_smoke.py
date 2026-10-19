"""Manual live smoke: lightweight planner -> arXiv -> ReferenceStore."""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from pathlib import Path

from ..config.factory import (
    build_model_registry,
    build_prompt_library,
    build_search_planner,
    load_config,
)
from ..persistence import ReferenceStore
from ..schemas import DatabaseSearchArguments, NoveltyPoint, ResearchTask, TaskResearchRequest
from ..tools.database_search.factory import build_database_search_tool
from ._support import minimal_search_plan


class RecordingPlanner:
    def __init__(self, planner) -> None:
        self.planner = planner
        self.plans = []

    def plan(self, point, task):
        plan = self.planner.plan(point, task)
        self.plans.append(plan)
        return plan


async def run(output_root: Path) -> dict[str, object]:
    config = load_config()
    models = build_model_registry(config)
    prompts = build_prompt_library()
    planner_alias = config["agents"]["search_planner"]["model"]
    planner = RecordingPlanner(build_search_planner(config, models, prompts))
    store = ReferenceStore(output_root)
    retrieval = config["retrieval"]
    tool = build_database_search_tool(
        retrieval,
        search_planner=planner,
        reference_store=store,
        max_concurrency=int(config.get("workflow", {}).get("max_concurrency", 4)),
    )
    scope = TaskResearchRequest(
        subject_paper_id="database-search-live-smoke",
        run_id="database-search-live-smoke-run",
        novelty_point=NoveltyPoint(
            point_id="NP-live",
            claim="Temporal graph neural networks using dynamic graph summarization",
            claim_en="Temporal graph neural networks using dynamic graph summarization",
            technical_features=["temporal graph neural network", "graph summarization"],
        ),
        research_task=ResearchTask(
            task_id="T-live",
            novelty_point_id="NP-live",
            task_type="search",
            language="en",
        ),
        search_plan=minimal_search_plan("T-live", "NP-live"),
    )
    started = time.monotonic()
    observation = await tool.ainvoke(DatabaseSearchArguments(source_id="arxiv"), scope=scope)
    manifest = store.load_manifest(scope.subject_paper_id)
    return {
        "planner_model": planner_alias,
        "source_id": "arxiv",
        "search_plans": [plan.model_dump(mode="json") for plan in planner.plans],
        "search_executions": observation.payload["search_executions"],
        "candidate_count": len(observation.payload["database_search_result"]["results"]),
        "work_count": len(manifest.works),
        "source_record_count": len(manifest.source_records),
        "artifact_count": len(manifest.artifacts),
        "warnings": observation.payload["database_search_result"]["warnings"],
        "elapsed_seconds": round(time.monotonic() - started, 3),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-root", type=Path, default=Path("outputs"))
    args = parser.parse_args()
    print(json.dumps(asyncio.run(run(args.output_root)), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
