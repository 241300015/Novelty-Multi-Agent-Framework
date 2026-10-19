"""M5 诊断：Planner 完整提示词 + 模型调用隔离测试。
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

from backend.env import ChatMessage, ModelCallOptions, ModelClient, PromptLibrary
from backend.env.model_client import _load_dev_env
from novelty_agent_framework.config import build_model_registry, load_application_config
from novelty_agent_framework.schemas import NoveltyPoint, PaperInput, ResearchTask
from novelty_agent_framework.schemas.search_plan_draft import SearchPlanDraft

PROMPTS_ROOT = Path("backend/src/novelty_agent_framework/prompts")
PAPER_INPUT = Path("outputs/MF2033k6lC/paper-input/others/paper.json")
POINTS_INPUT = Path("outputs/MF2033k6lC/novelty-points.json")


def main() -> None:
    _load_dev_env()
    config = load_application_config()
    registry = build_model_registry(config)
    client = registry.client_for("deepseek-flash")
    paper = PaperInput.model_validate_json(PAPER_INPUT.read_text(encoding="utf-8"))
    payload = json.loads(POINTS_INPUT.read_text(encoding="utf-8"))
    points = [NoveltyPoint.model_validate(item) for item in payload["novelty_points"]]
    point = next(item for item in points if item.point_id == "NP-1")
    task = ResearchTask(task_id="T-1", novelty_point_id="NP-1", task_type="literature_search", language="zh", attempt=1)
    rendered = PromptLibrary(PROMPTS_ROOT).render(
        "search_planner/plan",
        point_json=json.dumps(point.model_dump(mode="json"), ensure_ascii=False),
        task_json=json.dumps(task.model_dump(mode="json"), ensure_ascii=False),
        draft_schema=json.dumps(SearchPlanDraft.model_json_schema(), ensure_ascii=False),
        retry_reason="无（首次生成）",
    )
    print(json.dumps({
        "system_chars": len(rendered.system),
        "user_chars": len(rendered.user),
        "total_chars": len(rendered.system) + len(rendered.user),
    }), flush=True)
    started = time.perf_counter()
    try:
        response = client.complete(
            [ChatMessage(role="system", content=rendered.system),
             ChatMessage(role="user", content=rendered.user)],
            options=ModelCallOptions(temperature=0.2, max_tokens=2048, response_format={"type": "json_object"}),
        )
        elapsed = round(time.perf_counter() - started, 2)
        print(json.dumps({"ok": True, "elapsed_seconds": elapsed, "content": response.content[:200]}), flush=True)
    except Exception as exc:
        elapsed = round(time.perf_counter() - started, 2)
        print(json.dumps({"ok": False, "elapsed_seconds": elapsed, "error": str(exc)[:200]}), flush=True)


if __name__ == "__main__":
    main()