import json
from pathlib import Path

import pytest

from backend.env import ModelResponse, PromptLibrary
from novelty_agent_framework.agents import (
    NoveltyCoordinatorAgent,
    NoveltyResearchAgent,
)
from novelty_agent_framework.ports import SearchHit
from novelty_agent_framework.schemas import (
    InsufficientFinalEvidence,
    NoveltyPoint,
    PaperInput,
    ResearchTask,
)

PROMPTS_ROOT = Path("backend/src/novelty_agent_framework/prompts")


class RecordingModelClient:
    """记录调用消息并返回预设 JSON 的假模型客户端。"""

    def __init__(self, content: str) -> None:
        self.content = content
        self.calls: list[tuple[list, object]] = []

    def complete(self, messages, *, options=None):
        self.calls.append((list(messages), options))
        if options and options.response_format and options.response_format.get('type') == 'json_schema':
            choices = options.response_format['json_schema']['schema']['properties']['option_id']['enum']
            return ModelResponse(content=json.dumps({'option_id': choices[-1]}))
        return ModelResponse(content=self.content)


TASKS_JSON = json.dumps(
    [
        {
            "task_id": "MODEL-TASK-ID",
            "novelty_point_id": "NP-1",
            "task_type": "feature_supplement",
            "language": "en",
            "description": "补充多智能体协作特征的英文文献。",
            "attempt": 99,
        }
    ]
)


POINTS = [
    NoveltyPoint(
        point_id="NP-1",
        claim="提出一个测试查新点",
        technical_features=["多智能体协作"],
        source_locations=["abstract"],
    )
]


CARD_JSON = {
    "card_id": "CARD-1",
    "task_id": "TASK-NP-1-R1",
    "novelty_point_id": "NP-1",
    "document_title": "相关工作",
    "main_contribution": "提出了一个相关方法。",
    "overlaps": ["技术路线部分重合"],
    "differences": ["目标论文声明了不同组合"],
    "sources": [
        {
            "title": "相关工作",
            "quote": "原文摘录",
            "location": "Section 3",
            "url": "https://example.org/paper",
        }
    ],
    "cited_by_paper": False,
    "possible_baseline": True,
    "relevance": 0.82,
    "confidence": 0.78,
}


def make_paper() -> PaperInput:
    return PaperInput(
        paper_id="paper-1",
        title="测试论文",
        abstract="测试论文摘要",
        full_text="测试论文正文",
    )


def test_coordinator_plan_is_deterministic_without_model_call():
    client = RecordingModelClient(TASKS_JSON)
    agent = NoveltyCoordinatorAgent(
        model_client=client,
        prompts=PromptLibrary(PROMPTS_ROOT),
    )

    brief = agent.plan(
        make_paper(),
        points=POINTS,
        attempt=1,
    )

    assert brief.novelty_points[0].point_id == "NP-1"
    assert [(task.task_id, task.language) for task in brief.research_tasks] == [
        ("T-1", "en"),
    ]
    assert all(task.task_type == "literature_search" for task in brief.research_tasks)
    assert all(task.attempt == 1 for task in brief.research_tasks)
    assert brief.paper_summary == "测试论文摘要"
    assert client.calls == []


def test_coordinator_plan_creates_one_english_task_per_point_without_client():
    points = [
        *POINTS,
        NoveltyPoint(point_id="NP-2", claim="第二个查新点"),
    ]

    brief = NoveltyCoordinatorAgent().plan(make_paper(), points=points, attempt=3)

    assert [
        (task.novelty_point_id, task.task_id, task.language)
        for task in brief.research_tasks
    ] == [
        ("NP-1", "T-1", "en"),
        ("NP-2", "T-1", "en"),
    ]
    assert all(task.attempt == 3 for task in brief.research_tasks)


def test_coordinator_plan_fills_keywords_from_paper():
    paper = make_paper()
    paper.keywords_zh = ["图神经网络"]
    paper.keywords_en = ["graph neural network"]
    agent = NoveltyCoordinatorAgent(
        model_client=RecordingModelClient(TASKS_JSON),
        prompts=PromptLibrary(PROMPTS_ROOT),
    )

    brief = agent.plan(paper, points=POINTS, attempt=1)

    assert brief.keywords_zh == ["图神经网络"]
    assert brief.keywords_en == ["graph neural network"]


def test_coordinator_supplement_renders_prompt_from_library():
    """补充检索应能加载 prompts/coordinator/supplement.md（曾因文件名不匹配失败）。"""

    from novelty_agent_framework.schemas import NoveltyBrief

    brief = NoveltyBrief(
        paper_summary="测试论文摘要",
        research_problem="测试论文",
        novelty_points=list(POINTS),
        keywords_zh=["多智能体"],
        keywords_en=["multi-agent"],
        research_tasks=[],
    )
    client = RecordingModelClient(json.dumps({"trigger_code": "evidence.missing", "decisions": [{"point_id": "NP-1",
        "action": "research_gap", "reason": "Need additional comparison evidence",
        "research_tasks": [{**t, "task_type": "research_gap"} for t in json.loads(TASKS_JSON)]}]}))
    agent = NoveltyCoordinatorAgent(
        model_client=client,
        prompts=PromptLibrary(PROMPTS_ROOT),
    )

    result = agent.plan_supplement(
        make_paper(),
        brief=brief,
        existing_evidence=[],
        insufficient_final_evidence_points=[
            InsufficientFinalEvidence(
                novelty_point_id="NP-1",
                valid_card_count=0,
                required_card_count=1,
            )
        ],
        attempt=2,
    )

    result = result.brief
    assert result.research_tasks[0].task_id == "T-R2-COUNT-1"
    assert result.research_tasks[0].attempt == 2
    assert result.research_tasks[0].task_type == "research_gap"
    assert result.novelty_points == brief.novelty_points
    assert result.paper_summary == brief.paper_summary
    assert result.keywords_zh == brief.keywords_zh
    assert result.keywords_en == brief.keywords_en
    messages, _ = client.calls[0]
    assert "最终有效 EvidenceCard 数量" in messages[1].content
    assert "SearchPlan" in messages[1].content


def test_research_renders_prompt_and_validates_cards():
    client = RecordingModelClient(json.dumps([CARD_JSON]))
    agent = NoveltyResearchAgent(
        model_client=client,
        prompts=PromptLibrary(PROMPTS_ROOT),
    )
    task = ResearchTask(
        task_id="TASK-NP-1-R1",
        novelty_point_id="NP-1",
        task_type="literature_search",
        language="zh",
        description="检索多智能体协作文献",
        attempt=1,
    )

    cards = agent.research(
        task,
        POINTS[0],
        [
            SearchHit(
                document_id="DOC-1",
                title="相关工作",
                abstract="原文摘录",
                url="https://example.org/paper",
            )
        ],
    )

    assert len(cards) == 1
    assert cards[0].card_id == "CARD-1"
    messages, _ = client.calls[0]
    system, user = messages[0].content, messages[1].content
    assert "文献证据分析 Agent" in system
    assert '"task_id": "TASK-NP-1-R1"' in user
    assert "当前查新点" in user
    assert "候选文献" in user


def test_research_rejects_malformed_card():
    bad_card = dict(CARD_JSON)
    bad_card["relevance"] = 1.5  # 超出 0..1 范围
    client = RecordingModelClient(json.dumps([bad_card]))
    agent = NoveltyResearchAgent(
        model_client=client,
        prompts=PromptLibrary(PROMPTS_ROOT),
    )
    task = ResearchTask(
        task_id="TASK-NP-1-R1",
        novelty_point_id="NP-1",
        task_type="literature_search",
        language="zh",
        attempt=1,
    )

    cards = agent.research(
        task,
        POINTS[0],
        [
            SearchHit(
                document_id="DOC-1",
                title="相关工作",
                abstract="原文摘录",
                url="https://example.org/paper",
            )
        ],
    )

    assert cards == []


def test_agent_without_client_or_registry_raises():
    agent = NoveltyCoordinatorAgent()
    brief = agent.plan(make_paper(), points=POINTS, attempt=1)
    assert len(brief.research_tasks) == 1


def test_plan_supplement_rejects_task_for_unknown_point():
    tasks_json = json.dumps(
        [
            {
                "task_id": "TASK-X",
                "novelty_point_id": "NP-99",
                "task_type": "literature_search",
                "language": "en",
                "description": "补检",
                "attempt": 2,
            }
        ]
    )
    agent = NoveltyCoordinatorAgent(
        model_client=RecordingModelClient(json.dumps({"trigger_code": "evidence.missing", "decisions": [{"point_id": "NP-1",
            "action": "research_gap", "reason": "Need evidence", "research_tasks": json.loads(tasks_json)}]})),
        prompts=PromptLibrary(PROMPTS_ROOT),
    )

    from novelty_agent_framework.schemas import NoveltyBrief

    brief = NoveltyBrief(
        paper_summary="摘要",
        novelty_points=list(POINTS),
        research_tasks=[],
    )
    result = agent.plan_supplement(
            make_paper(),
            brief=brief,
            existing_evidence=[],
            insufficient_final_evidence_points=[
                InsufficientFinalEvidence(
                    novelty_point_id="NP-1",
                    valid_card_count=0,
                    required_card_count=1,
                )
            ],
            attempt=2,
        )

    assert result.recovery_decisions[0].coordinator_status == 'failed'
    assert result.brief.research_tasks == []
