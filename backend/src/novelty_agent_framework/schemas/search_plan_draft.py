"""SearchPlanner 的两阶段模型契约与内部编译契约。

只包含必须由 LLM 生成的语义载荷：concepts（role/terms/alias/exclude/importance）
与 strategies（level + 可选 focus_concepts），分别在两个阶段返回。布尔表达式由
agents/search_plan_compiler.py 按模板确定性生成，LLM 不再输出 expression。
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from .domain import StrictModel


class SearchConceptDraft(StrictModel):
    """一个语义概念：角色、规范词项、别名、排除词与重要性。

    concept_id 由系统按顺序分配 C1..Cn；terms 为 3~7 词名词短语（≤3 个），
    alias 为同义/缩写/跨领域叫法（非 paraphrase，≤3 个），
    exclude 为 NOT 词（≤2 个），importance 1~3（3=机制/方法，1=场景/应用）。
    """

    role: Literal["object", "method", "feature", "setting", "escape"] = "object"
    terms: list[str] = Field(min_length=1)
    alias: list[str] = Field(default_factory=list)
    exclude: list[str] = Field(default_factory=list)
    importance: int = Field(default=2, ge=1, le=3)


class SearchStrategyDraft(StrictModel):
    """一条检索策略意图；strategy_id 与 expression 由系统生成。

    level 在 strict/medium/broad 中取值；focus_concepts 可选，引用 C1..Cn，
    非空时覆盖模板的默认概念选择（仅在确有必要时使用）。
    """

    level: Literal["strict", "medium", "broad"]
    focus_concepts: list[str] = Field(default_factory=list)


class SearchPlanDraft(StrictModel):
    """Harness 合并两个阶段后交给编译器的内部草稿，兼容历史归档。

    - concepts 按顺序编号为 C1..Cn；
    - strategies 数量：literature_search 必须恰好三条（strict/medium/broad），
      补检任务 1~3 条且 level 不重复；
    - 表达式由编译器按模板组装，LLM 不生成布尔表达式。
    """

    concepts: list[SearchConceptDraft] = Field(min_length=1)
    strategies: list[SearchStrategyDraft] = Field(min_length=1)


class SearchConceptsDraft(StrictModel):
    """Concept-stage output. Strategies are not editable in this stage."""

    concepts: list[SearchConceptDraft] = Field(min_length=1)


class SearchStrategiesDraft(StrictModel):
    """Strategy-stage output. Concepts are supplied and frozen by Harness."""

    strategies: list[SearchStrategyDraft] = Field(min_length=1)
