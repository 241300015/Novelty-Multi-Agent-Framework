"""Coordinator JSON 解析韧性与 synthesize 重试契约。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend.env import ModelResponse, PromptLibrary
from novelty_agent_framework.agents import NoveltyCoordinatorAgent
from novelty_agent_framework.schemas import (
    NoveltyBrief,
    NoveltyPoint,
    NoveltyPointReview,
    NoveltyReport,
    NoveltyVerdict,
    PaperInput,
    ReviewStatus,
)

PROMPTS_ROOT = Path("backend/src/novelty_agent_framework/prompts")


class SequenceClient:
    """按顺序返回预设内容的模型客户端，并记录调用次数。"""

    def __init__(self, *contents: str) -> None:
        self.contents = list(contents)
        self.calls = 0

    def complete(self, messages, *, options=None):
        content = self.contents[min(self.calls, len(self.contents) - 1)]
        self.calls += 1
        if self.calls > 1:
            try:
                draft = json.loads(content)
                feedback = json.loads(messages[-1].content)
                if set(draft) == {"summary"} and "fields" in feedback:
                    content = json.dumps({"option_id": next(iter(feedback["options"])),
                        "updates": {key: None if spec["operation"] == "remove" else draft["summary"]
                                    for key, spec in feedback["fields"].items()}})
            except ValueError:
                pass
        return ModelResponse(content=content)


def _paper() -> PaperInput:
    return PaperInput(paper_id="paper-1", title="测试论文", full_text="正文")


def _brief() -> NoveltyBrief:
    return NoveltyBrief(
        paper_summary="摘要",
        research_problem="测试论文",
        novelty_points=[
            NoveltyPoint(point_id="NP-1", claim="claim", claim_en="claim_en")
        ],
        research_tasks=[],
    )


def _report_json() -> str:
    return json.dumps({"summary": "存在部分技术差异"}, ensure_ascii=False)


def _review() -> NoveltyPointReview:
    return NoveltyPointReview(
        novelty_point_id="NP-1",
        status=ReviewStatus.REVIEWED,
        verdict=NoveltyVerdict.PARTIALLY_NOVEL,
        verdict_reason="存在已知重合，但完整组合仍有差异。",
        confidence=0.8,
    )


def _agent(client: SequenceClient) -> NoveltyCoordinatorAgent:
    return NoveltyCoordinatorAgent(
        model_client=client,
        prompts=PromptLibrary(PROMPTS_ROOT),
    )


def test_synthesize_accepts_markdown_fenced_json() -> None:
    prefix = "```json\n"
    suffix = "\n```"
    client = SequenceClient(prefix + _report_json() + suffix)
    report = _agent(client).synthesize(
        _paper(),
        brief=_brief(),
        evidence=[],
        novelty_reviews=[_review()],
        rejected_evidence=[],
        insufficient_final_evidence_points=[],
    )
    assert isinstance(report, NoveltyReport)
    assert report.paper_id == "paper-1"
    assert report.conclusions[0].verdict is NoveltyVerdict.PARTIALLY_NOVEL
    assert client.calls == 1


def test_synthesize_retries_once_when_first_response_is_not_json() -> None:
    client = SequenceClient("抱歉，输出如下：\n不是 JSON", _report_json())
    report = _agent(client).synthesize(
        _paper(),
        brief=_brief(),
        evidence=[],
        novelty_reviews=[_review()],
        rejected_evidence=[],
        insufficient_final_evidence_points=[],
    )
    assert isinstance(report, NoveltyReport)
    assert client.calls == 2


def test_synthesize_fails_after_bounded_attempts() -> None:
    client = SequenceClient("still not json", "still not json")
    agent = _agent(client)
    with pytest.raises(ValueError, match="draft_contract_error"):
        agent.synthesize(
            _paper(),
            brief=_brief(),
            evidence=[],
            novelty_reviews=[_review()],
            rejected_evidence=[],
            insufficient_final_evidence_points=[],
        )
    assert client.calls == 3
