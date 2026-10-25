"""英文检索内容与中文解释在真实模型调用边界分别约束。"""
from pathlib import Path

import pytest

from backend.env import ModelResponse, PromptLibrary
from novelty_agent_framework.agents import NoveltyCoordinatorAgent
from novelty_agent_framework.response_language import CHINESE_EXPLANATIONS
from novelty_agent_framework.schemas import NoveltyBrief, PaperInput
from test_report_narrative_contract import _case, _Client


@pytest.mark.parametrize("use_templates", [False, True])
def test_english_research_requests_chinese_narrative_with_templates_and_fallback(use_templates):
    points, cards, reviews, draft = _case(1)
    draft.conclusions[0].summary = "已有文献覆盖部分方法，现有比较支持所述差异。"
    client = _Client([ModelResponse(content=draft.model_dump_json())])
    prompts = PromptLibrary(Path(__file__).resolve().parents[1] / "backend/src/novelty_agent_framework/prompts") if use_templates else None
    agent = NoveltyCoordinatorAgent(model_client=client, prompts=prompts, research_languages=("en",))
    paper = PaperInput(paper_id="language-test", title="English graph research", abstract="English abstract.", full_text="English body.")
    report = agent.synthesize(paper, brief=NoveltyBrief(paper_summary="English context", research_problem="Graph learning", novelty_points=points),
                             evidence=cards, novelty_reviews=reviews, rejected_evidence=[], insufficient_final_evidence_points=[])
    messages, _ = client.calls[0]
    assert CHINESE_EXPLANATIONS in messages[0].content
    assert "English graph research" in messages[1].content
    assert "English abstract." in messages[1].content
    assert "ResearchTask.language" in messages[0].content
    assert report.conclusions[0].summary == draft.conclusions[0].summary
    assert agent.research_languages == ("en",)
    if use_templates:
        assert "综述中不出现工具调用、补检次数、预算、错误码" in messages[0].content


def test_research_workflow_requests_chinese_explanations_for_english_task():
    import asyncio
    from test_task_researcher_workflow import FakeModel, FakeBuilder, finish, scope
    from novelty_agent_framework.tools import ResearcherToolRegistry
    from novelty_agent_framework.workflows import TaskResearcherWorkflow

    model = FakeModel([finish()])
    request = scope()
    workflow = TaskResearcherWorkflow(model, ResearcherToolRegistry(), FakeBuilder())
    asyncio.run(workflow.ainvoke(request))
    system = model.messages[0][0].content
    assert request.research_task.language == "en"
    assert CHINESE_EXPLANATIONS in system
    assert "原文引句和 exact_quote 保持原文" in system
    assert "JSON 字段名、枚举值、ID、工具名、选项标签" in system
