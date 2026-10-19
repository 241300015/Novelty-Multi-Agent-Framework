"""M5 探针：单任务 Planner 直连测试（NP-1/T-1，v2 契约）。
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

from backend.env.model_client import _load_dev_env
from novelty_agent_framework.config import build_workflow, load_application_config
from novelty_agent_framework.schemas import NoveltyPoint, PaperInput

PAPER_INPUT = Path("outputs/MF2033k6lC/paper-input/others/paper.json")
POINTS_INPUT = Path("outputs/MF2033k6lC/novelty-points.json")


def main() -> None:
    _load_dev_env()
    config = load_application_config()
    paper = PaperInput.model_validate_json(PAPER_INPUT.read_text(encoding="utf-8"))
    payload = json.loads(POINTS_INPUT.read_text(encoding="utf-8"))
    points = [NoveltyPoint.model_validate(item) for item in payload["novelty_points"]]
    point = next(item for item in points if item.point_id == "NP-1")
    built = build_workflow(config)
    brief = built.services.coordinator.plan(paper, points=points, attempt=1)
    task = next(item for item in brief.research_tasks if item.novelty_point_id == "NP-1")
    planner = built.services.search_planner
    print(f"planner model: {planner._model_alias or 'registry'}", file=sys.stderr)
    started = time.perf_counter()
    plan = planner.plan(point, task)
    elapsed = round(time.perf_counter() - started, 2)
    print(json.dumps({
        "elapsed_seconds": elapsed,
        "task": f"{task.novelty_point_id}/{task.task_id}",
        "concepts": [
            {
                "id": c.concept_id,
                "name": c.name,
                "role": c.role,
                "terms": c.terms,
                "alias": c.alias,
                "importance": c.importance,
            }
            for c in plan.concepts
        ],
        "strategies": [
            {
                "id": s.strategy_id,
                "level": s.level,
                "expression": s.expression,
                "use_alias": s.use_alias,
                "use_exclude": s.use_exclude,
            }
            for s in plan.strategies
        ],
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()