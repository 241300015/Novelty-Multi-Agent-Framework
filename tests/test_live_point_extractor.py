"""真实 R1 查新点提取冒烟测试（默认跳过，需 -m live）。"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from backend.env import ModelProfile, OpenAICompatibleChatClient, PromptLibrary
from novelty_agent_framework.agents import (
    NoveltyPointExtractorAgent,
    build_paper_digest,
)
from novelty_agent_framework.schemas import PaperInput
from novelty_agent_framework.persistence import persist_workflow_input

pytestmark = pytest.mark.live

PAPER_JSON = Path("outputs/MF2033k6lC/paper-input/others/paper.json")
PROMPTS_ROOT = Path("backend/src/novelty_agent_framework/prompts")


@pytest.mark.skipif(
    not os.getenv("SILICONFLOW_API_KEY"),
    reason="需要 SILICONFLOW_API_KEY（backend/.env 或环境变量）",
)
def test_live_r1_extracts_points(tmp_path) -> None:
    data = json.loads(PAPER_JSON.read_text(encoding="utf-8"))
    paper = PaperInput.model_validate(data)
    _, reference = persist_workflow_input(paper, output_root=tmp_path, with_reading_reference=True)
    digest = build_paper_digest(paper).model_copy(update={"paper_source": reference})

    profile = ModelProfile(
        alias="r1-qwen3-8b",
        model="deepseek-ai/DeepSeek-R1-0528-Qwen3-8B",
        base_url="https://api.siliconflow.cn/v1",
        api_key=os.getenv("SILICONFLOW_API_KEY"),
        defaults={"timeout_seconds": 180},
    )
    agent = NoveltyPointExtractorAgent(
        model_client=OpenAICompatibleChatClient(profile),
        prompts=PromptLibrary(PROMPTS_ROOT),
    )

    points = agent.extract(digest, previous_brief=None, attempt=1)

    assert 1 <= len(points) <= 8
    assert all(point.claim for point in points)
    print("\n提取到", len(points), "个查新点：")
    for point in points:
        print(" -", point.claim)
