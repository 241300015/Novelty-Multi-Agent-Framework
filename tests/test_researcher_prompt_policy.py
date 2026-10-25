from pathlib import Path

from backend.env import PromptLibrary

from novelty_agent_framework.schemas import NoveltyPoint
from novelty_agent_framework.workflows.research_task import _project_novelty_point


PROMPTS_ROOT = Path("backend/src/novelty_agent_framework/prompts")


def render_prompt():
    return PromptLibrary(PROMPTS_ROOT).render(
        "research/native_tool_loop",
        novelty_point_json='{"point_id":"NP-1"}',
        research_task_json='{"task_id":"T-1","language":"zh"}',
        search_plan_json='{"concepts":[{"terms":["term"]}],"strategies":[{"expression":"C1"}]}',
        finish_schema_json='{"type":"object"}',
    )


def test_researcher_prompt_renders_attempt3_retrieval_policy() -> None:
    rendered = render_prompt()

    assert rendered.version == "5"
    assert "Rely on database_search for scholarly discovery" in rendered.system
    assert "language alone is not a trigger" in rendered.system
    assert "Search results and snippets are discovery metadata, not evidence" in rendered.system
    assert "Never issue consecutive web_search calls" in rendered.system
    assert "Never guess an\nunlisted database source_id" in rendered.system
    assert "the next tool call MUST be reader" in rendered.system
    assert "Do not call database_search, web_search, or browser again" in " ".join(rendered.system.split())
    assert "When reference_search is available" in rendered.system
    assert "Do not decide whether a source is evidentiary based only on search snippets" in rendered.system


def test_researcher_prompt_renders_exact_quote_and_empty_finish_policy() -> None:
    system = render_prompt().system

    assert "copied verbatim from a successful Reader observation" in system
    assert "Do not paraphrase, summarize, translate, normalize, rewrite" in system
    assert "exact supporting span in Reader text" in system
    assert "cards=[] and a concrete no_evidence_reason" in system
    assert "Never force a card" in system


def test_novelty_point_projection_hides_source_locations() -> None:
    """回归守卫：source_locations 不保证逐字，不能暴露给 Researcher。

    PointExtractor 会顺手清理 PDF 抽取残留（实测把论文里的
    ``(model F _ { 2 } ) ) )`` 从引文中间去掉），于是 source_locations 里的
    「引文」并不逐字。模型看到这种带引号、还标了章节的现成字符串，会直接当作
    引文写进 finish draft，随后必然被判 ungrounded——引文只能来自成功的 Reader 观测。
    """

    point = NoveltyPoint(
        point_id="NP-1",
        claim="推理能力主要来自 o_proj",
        technical_features=["Delta Stethoscope"],
        source_locations=[
            "Section 2.3: 'simply tuning o_proj and layernorm leads to strong "
            "reasoning ability'"
        ],
    )

    projected = _project_novelty_point(point)

    assert "source_locations" not in projected
    assert projected["claim"] == "推理能力主要来自 o_proj"
    assert projected["technical_features"] == ["Delta Stethoscope"]
    assert projected["point_id"] == "NP-1"
