from pathlib import Path

import pytest

from backend.env import (
    PromptLibrary,
    PromptRenderError,
    PromptTemplate,
    parse_front_matter,
)


PLAN_TEMPLATE = """\
---
name: coordinator.plan
version: 1
system: |
  你是查新系统的 Coordinator。
  禁止编造文献。
---
请生成查新计划。
论文：{paper_json}
轮次：{attempt}
"""


def write_template(root: Path, name: str = "coordinator/plan") -> Path:
    path = root / f"{name}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(PLAN_TEMPLATE, encoding="utf-8")
    return path


def test_render_fills_variables(tmp_path):
    write_template(tmp_path)
    library = PromptLibrary(tmp_path)

    rendered = library.render(
        "coordinator/plan",
        paper_json='{"paper_id": "P001"}',
        attempt=1,
    )

    assert rendered.name == "coordinator.plan"
    assert rendered.version == "1"
    assert "禁止编造文献" in rendered.system
    assert '{"paper_id": "P001"}' in rendered.user
    assert "轮次：1" in rendered.user


def test_render_with_different_variables_is_not_cached_stale(tmp_path):
    write_template(tmp_path)
    library = PromptLibrary(tmp_path)

    first = library.render("coordinator/plan", paper_json="A", attempt=1)
    second = library.render("coordinator/plan", paper_json="B", attempt=2)

    assert "A" in first.user
    assert "B" in second.user
    assert "轮次：2" in second.user


def test_render_missing_variable_raises(tmp_path):
    write_template(tmp_path)
    library = PromptLibrary(tmp_path)

    with pytest.raises(PromptRenderError, match="缺少变量"):
        library.render("coordinator/plan", paper_json="A")


def test_missing_file_uses_fallback(tmp_path):
    library = PromptLibrary(
        tmp_path,
        fallbacks={
            "coordinator/plan": PromptTemplate(
                name="coordinator.plan",
                version="fallback",
                system="fallback system",
                user_template="fallback user {paper_json}",
            )
        },
    )

    rendered = library.render("coordinator/plan", paper_json="X")
    assert rendered.system == "fallback system"
    assert rendered.user == "fallback user X"


def test_missing_file_without_fallback_raises(tmp_path):
    library = PromptLibrary(tmp_path)
    with pytest.raises(PromptRenderError, match="不存在"):
        library.render("coordinator/plan")


def test_parse_front_matter_block_value():
    text = "---\nname: x\nsystem: |\n  第一行\n  第二行\n---\n正文\n"
    meta, body = parse_front_matter(text)
    assert meta["name"] == "x"
    assert meta["system"] == "第一行\n第二行"
    assert body == "正文"


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_multiline_system_keeps_paragraphs_indentation_and_next_metadata(newline):
    text = newline.join([
        "---", "name: x", "system: |", "  first", "", "  second:",
        "    nested example", "  ", "  last rule", "version: 2", "---", "body",
    ])
    meta, body = parse_front_matter(text)
    assert meta == {"name": "x", "version": "2",
                    "system": "first\n\nsecond:\n  nested example\n\nlast rule"}
    assert body == "body"


def test_real_planner_prompt_keeps_rules_and_examples_after_blank_lines():
    root = Path(__file__).resolve().parents[1] / "backend/src/novelty_agent_framework/prompts"
    rendered = PromptLibrary(root).render("search_planner/plan", point_json="{}",
        task_json="{}", retry_reason="无", draft_schema="{}")
    for section in ("## 角色", "## 概念", "## 策略", "## 输出要求", "## 正例", "## 反例"):
        assert section in rendered.system
    assert "全部 terms、alias、exclude" in rendered.system
