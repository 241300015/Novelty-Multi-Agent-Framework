"""M5 诊断：Planner 完整载荷 + urllib 直连（绕开 ModelClient）。
"""

from __future__ import annotations

import json
import os
import time
import urllib.request
from pathlib import Path

from backend.env import PromptLibrary
from backend.env.model_client import _load_dev_env
from novelty_agent_framework.schemas import NoveltyPoint, PaperInput, ResearchTask
from novelty_agent_framework.schemas.search_plan_draft import SearchPlanDraft

PROMPTS_ROOT = Path("backend/src/novelty_agent_framework/prompts")
PAPER_INPUT = Path("outputs/MF2033k6lC/paper-input/others/paper.json")
POINTS_INPUT = Path("outputs/MF2033k6lC/novelty-points.json")


def main() -> None:
    _load_dev_env()
    _KEY = os.environ.get("SILICONFLOW_API_KEY", "")
    paper = PaperInput.model_validate_json(PAPER_INPUT.read_text(encoding="utf-8"))
    payload = json.loads(POINTS_INPUT.read_text(encoding="utf-8"))
    point = next(NoveltyPoint.model_validate(item) for item in payload["novelty_points"] if item["point_id"] == "NP-1")
    task = ResearchTask(task_id="T-1", novelty_point_id="NP-1", task_type="literature_search", language="zh", attempt=1)
    rendered = PromptLibrary(PROMPTS_ROOT).render(
        "search_planner/plan",
        point_json=json.dumps(point.model_dump(mode="json"), ensure_ascii=False),
        task_json=json.dumps(task.model_dump(mode="json"), ensure_ascii=False),
        draft_schema=json.dumps(SearchPlanDraft.model_json_schema(), ensure_ascii=False),
        retry_reason="无（首次生成）",
    )
    body = json.dumps({
        "model": "deepseek-ai/DeepSeek-V4-Flash",
        "messages": [
            {"role": "system", "content": rendered.system},
            {"role": "user", "content": rendered.user},
        ],
        "response_format": {"type": "json_object"},
        "max_tokens": 2048,
        "temperature": 0.2,
        "enable_thinking": False,
    }).encode("utf-8")
    print(json.dumps({"body_chars": len(body)}), flush=True)
    started = time.perf_counter()
    try:
        request = urllib.request.Request(
            "https://api.siliconflow.cn/v1/chat/completions",
            data=body,
            headers={
                "Authorization": f"Bearer {_KEY}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=90) as response:
            raw = response.read().decode("utf-8")
        elapsed = round(time.perf_counter() - started, 2)
        print(json.dumps({"ok": True, "elapsed_seconds": elapsed, "sample": raw[:150]}), flush=True)
    except Exception as exc:
        elapsed = round(time.perf_counter() - started, 2)
        print(json.dumps({"ok": False, "elapsed_seconds": elapsed, "error": str(exc)[:150]}), flush=True)


if __name__ == "__main__":
    main()