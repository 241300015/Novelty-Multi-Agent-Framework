"""SearchPlan 补全器（build_runtime_plan）v2 的独立单测。

覆盖：身份/编号注入、模板表达式形状、语义校验与结构化错误。
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from novelty_agent_framework.agents.search_plan_compiler import (
    SemanticLimits,
    DraftIssue,
    SearchPlanCompilationError,
    build_runtime_plan,
    validate_draft_semantics,
)
from novelty_agent_framework.core.search_plan_expression import (
    parse_search_plan_expression,
)
from novelty_agent_framework.schemas import ResearchTask
from novelty_agent_framework.schemas.search_plan_draft import (
    SearchConceptDraft,
    SearchPlanDraft,
    SearchStrategyDraft,
)


def make_task(task_type: str = "literature_search", language: str = "zh") -> ResearchTask:
    return ResearchTask(
        task_id="T-1",
        novelty_point_id="NP-1",
        task_type=task_type,
        language=language,
        description="针对该查新点执行检索。",
        attempt=1,
    )


def make_draft(*, concepts=None, strategies=None) -> SearchPlanDraft:
    concepts = concepts or [
        {"role": "object", "terms": ["图自编码器"], "alias": ["GAE"], "importance": 3},
        {"role": "method", "terms": ["循环神经网络"], "alias": ["RNN"], "importance": 3},
        {"role": "escape", "terms": ["动态时序图处理"], "importance": 2},
    ]
    strategies = strategies or [
        {"level": "strict"},
        {"level": "medium"},
        {"level": "broad"},
    ]
    return SearchPlanDraft(
        concepts=[SearchConceptDraft(**concept) for concept in concepts],
        strategies=[SearchStrategyDraft(**strategy) for strategy in strategies],
    )


def test_runtime_plan_injects_identity_and_ids() -> None:
    plan = build_runtime_plan(make_draft(), task=make_task())
    assert plan.protected_concept_ids == ["C1"]
    assert plan.task_id == "T-1"
    assert plan.novelty_point_id == "NP-1"
    assert [c.concept_id for c in plan.concepts] == ["C1", "C2", "C3"]
    assert [c.name for c in plan.concepts] == ["图自编码器", "循环神经网络", "动态时序图处理"]
    assert [s.strategy_id for s in plan.strategies] == ["S1", "S2", "S3"]
    assert [s.level for s in plan.strategies] == ["strict", "medium", "broad"]


def test_concept_v2_fields_flow_into_runtime_plan() -> None:
    plan = build_runtime_plan(make_draft(), task=make_task())
    assert [c.role for c in plan.concepts] == ["object", "method", "escape"]
    assert plan.concepts[0].alias == ["GAE"]
    assert [c.importance for c in plan.concepts] == [3, 3, 2]


def test_template_expression_shapes() -> None:
    plan = build_runtime_plan(make_draft(), task=make_task())
    assert plan.strategies[0].expression == "C1 AND C2"  # strict: 高重要概念
    assert plan.strategies[1].expression == "C1 AND C2"  # medium: 前 2 高重要
    assert plan.strategies[2].expression == "C1"  # broad: 仅保留 anchor
    assert [s.use_alias for s in plan.strategies] == [False, True, True]


def test_importance_ordering_shapes_pools() -> None:
    draft = make_draft(
        concepts=[
            {"role": "object", "terms": ["对象A"], "importance": 3},
            {"role": "feature", "terms": ["特征B"], "importance": 1},
            {"role": "method", "terms": ["方法C"], "importance": 2},
            {"role": "escape", "terms": ["解法D"], "importance": 2},
        ]
    )
    plan = build_runtime_plan(draft, task=make_task())
    assert plan.strategies[0].expression == "C1"  # 仅 importance=3
    assert plan.strategies[1].expression == "C1"
    assert plan.strategies[2].expression == "C1"


def test_focus_concepts_override_default_pool() -> None:
    draft = make_draft(strategies=[
        {"level": "strict", "focus_concepts": ["C3"]},
        {"level": "medium", "focus_concepts": ["C2", "C3"]},
        {"level": "broad"},
    ])
    plan = build_runtime_plan(draft, task=make_task())
    assert plan.strategies[0].expression == "C1 AND C3"
    assert plan.strategies[1].expression == "C1 AND C3"


def test_description_uses_concept_names() -> None:
    plan = build_runtime_plan(make_draft(), task=make_task())
    assert plan.strategies[0].description == "图自编码器 AND 循环神经网络"
    assert plan.strategies[2].description == "图自编码器"


def test_terms_are_normalized() -> None:
    draft = make_draft(
        concepts=[{"role": "object", "terms": [" 图摘要 ", "图摘要 ", ""], "importance": 3}],
        strategies=[{"level": "strict"}],
    )
    plan = build_runtime_plan(draft, task=make_task(task_type="feature_supplement"))
    assert plan.concepts[0].terms == ["图摘要", "图摘要"]
    assert plan.concepts[0].name == "图摘要"


def test_supplement_tasks_allow_fewer_strategies() -> None:
    draft = make_draft(strategies=[{"level": "strict"}])
    plan = build_runtime_plan(draft, task=make_task(task_type="feature_supplement"))
    assert len(plan.strategies) == 1
    assert plan.strategies[0].level == "strict"


def test_semantics_issues_are_structured() -> None:
    draft = make_draft(strategies=[{"level": "strict"}])
    issues = validate_draft_semantics(draft, task=make_task())
    assert any(issue.code == "strategy_levels" for issue in issues)
    assert all(isinstance(issue, DraftIssue) for issue in issues)
    assert issues[0].detail and issues[0].fix


@pytest.mark.parametrize(
    ("mutate", "code"),
    [
        (lambda concepts: concepts[0].__setitem__("terms", ["efficient robust learning"]), "generic_term"),
        (lambda concepts: concepts[0].__setitem__("terms", ["a b c d e f g h i j"]), "sentence_like_term"),
        (lambda concepts: concepts[0].__setitem__("terms", [""]), "blank_term"),
        (lambda concepts: concepts[0].__setitem__("alias", ["x", "y", "z", "w", "v"]), "too_many_alias"),
        (lambda concepts: concepts[0].__setitem__("terms", ["t1", "t2", "t3", "t4", "t5", "t6"]), "too_many_terms"),
        (lambda concepts: concepts[0].__setitem__("exclude", ["a", "b", "c", "d"]), "too_many_exclude"),
    ],
)
def test_semantics_rejections(mutate, code) -> None:  # type: ignore[no-untyped-def]
    concepts = [
        {"role": "object", "terms": ["图自编码器"], "importance": 3},
        {"role": "method", "terms": ["循环神经网络"], "importance": 3},
        {"role": "escape", "terms": ["动态时序图处理"], "importance": 2},
    ]
    mutate(concepts)
    draft = make_draft(concepts=concepts)
    with pytest.raises(SearchPlanCompilationError) as excinfo:
        build_runtime_plan(draft, task=make_task())
    assert any(issue.code == code for issue in excinfo.value.issues)


def test_language_requirements() -> None:
    # en 任务全中文 → missing_en_term
    draft = make_draft()
    issues = validate_draft_semantics(draft, task=make_task(language="en"))
    assert any(issue.code == "missing_en_term" for issue in issues)

    # zh 任务全英文 → 允许（arXiv 检索只需英文；M5 真实实验校准）
    draft_en = make_draft(
        concepts=[
            {"role": "object", "terms": ["graph summarization"], "importance": 3},
            {"role": "escape", "terms": ["graph condensation"], "importance": 2},
        ]
    )
    issues = validate_draft_semantics(draft_en, task=make_task(language="zh"))
    assert not any(issue.code == "missing_zh_term" for issue in issues)

@pytest.mark.parametrize("field,term", [
    ("terms", "GSAERU模型"), ("terms", "动态图"),
    ("alias", "动态图 GNN"), ("exclude", "综述"),
])
def test_en_plan_rejects_each_non_english_field_even_with_other_english_terms(field, term):
    concept = {"role": "object", "terms": ["dynamic graph"], "alias": ["GNN"], "importance": 3}
    concept[field] = [*concept.get(field, []), term]
    draft = make_draft(concepts=[concept])
    issues = validate_draft_semantics(draft, task=make_task(language="en"))
    language_issue = next(issue for issue in issues if issue.code == "missing_en_term")
    assert language_issue.paths == (("concepts", 0, field),)
    assert language_issue.failure_code.value == "planner.draft.missing_en_term"
    with pytest.raises(SearchPlanCompilationError):
        build_runtime_plan(draft, task=make_task(language="en"))


def test_en_plan_accepts_technical_english_and_acronyms():
    draft = make_draft(concepts=[{"role": "object", "terms": ["dynamic graph", "GSAERU"],
                                 "alias": ["temporal GNN"], "exclude": ["survey"], "importance": 3}])
    assert not validate_draft_semantics(draft, task=make_task(language="en"))
    assert build_runtime_plan(draft, task=make_task(language="en")).concepts[0].terms == ["dynamic graph", "GSAERU"]


def test_undefined_focus_concept_rejected() -> None:
    draft = make_draft(
        strategies=[
            {"level": "strict", "focus_concepts": ["C9"]},
            {"level": "medium"},
            {"level": "broad"},
        ]
    )
    with pytest.raises(SearchPlanCompilationError, match="undefined_concept"):
        build_runtime_plan(draft, task=make_task())


def test_too_many_concepts_rejected() -> None:
    concepts = [
        {"role": "object", "terms": [f"对象{i}"], "importance": 2}
        for i in range(6)
    ]
    concepts.append({"role": "escape", "terms": ["解法"], "importance": 2})
    draft = make_draft(concepts=concepts)
    with pytest.raises(SearchPlanCompilationError, match="too_many_concepts"):
        build_runtime_plan(draft, task=make_task())


def test_draft_schema_requires_at_least_one_concept() -> None:
    with pytest.raises(ValidationError):
        SearchPlanDraft(
            concepts=[],
            strategies=[SearchStrategyDraft(level="strict")],
        )




def test_escape_required_only_when_configured() -> None:
    draft = make_draft(
        concepts=[
            {"role": "object", "terms": ["图自编码器"], "importance": 3},
            {"role": "method", "terms": ["循环神经网络"], "importance": 3},
        ]
    )
    # 默认 require_escape=False：缺 escape 不阻断（真实模型校准）
    plan = build_runtime_plan(draft, task=make_task())
    assert [c.role for c in plan.concepts] == ["object", "method"]

    # require_escape=True：缺 escape 拒绝
    with pytest.raises(SearchPlanCompilationError, match="missing_escape"):
        build_runtime_plan(
            draft, task=make_task(), limits=SemanticLimits(require_escape=True)
        )

def test_custom_semantic_limits_override_defaults() -> None:
    draft = make_draft()  # 3 概念
    strict_limits = SemanticLimits(max_concepts=2)
    issues = validate_draft_semantics(
        draft, task=make_task(), limits=strict_limits
    )
    assert any(issue.code == "too_many_concepts" for issue in issues)

    loose_issues = validate_draft_semantics(draft, task=make_task())
    assert not any(issue.code == "too_many_concepts" for issue in loose_issues)


def test_focus_concepts_preserve_anchor_and_monotonicity() -> None:
    """显式 focus 仍保留编译阶段选定的 anchor。"""

    draft = make_draft(
        concepts=[
            {"role": "object", "terms": ["图自编码器"], "importance": 3},
            {"role": "method", "terms": ["循环神经网络"], "importance": 3},
            {"role": "escape", "terms": ["动态时序图处理"], "importance": 2},
        ],
        strategies=[
            {"level": "strict", "focus_concepts": ["C3"]},
            {"level": "medium"},
            {"level": "broad"},
        ],
    )
    plan = build_runtime_plan(draft, task=make_task())
    assert plan.strategies[0].expression == "C1 AND C3"

def test_semantic_limits_defaults_match_config_file() -> None:
    """代码默认值与配置文件必须一致，防止 prompt/配置/代码三处漂移。"""

    import json
    from pathlib import Path

    config_path = (
        Path("backend/src/novelty_agent_framework/config/agents")
        / "search_planner.example.json"
    )
    limits_config = json.loads(config_path.read_text(encoding="utf-8"))["limits"]
    defaults = SemanticLimits()
    for field in (
        "max_concepts",
        "max_terms_per_concept",
        "max_alias_per_concept",
        "max_exclude_per_concept",
        "max_term_words",
    ):
        assert getattr(defaults, field) == limits_config[field], field


def test_compiled_expressions_conform_to_shared_dsl() -> None:
    """模板/覆盖生成的表达式必须通过共享 DSL 语法校验（防御性守卫契约）。"""

    drafts_and_tasks = [
        (make_draft(), make_task()),
        (
            make_draft(
                strategies=[
                    {"level": "strict", "focus_concepts": ["C1", "C2"]},
                    {"level": "medium"},
                    {"level": "broad"},
                ]
            ),
            make_task(),
        ),
        (
            make_draft(strategies=[{"level": "strict"}]),
            make_task(task_type="feature_supplement"),
        ),
    ]
    for draft, task in drafts_and_tasks:
        plan = build_runtime_plan(draft, task=task)
        defined = {concept.concept_id for concept in plan.concepts}
        for strategy in plan.strategies:
            tokens = parse_search_plan_expression(
                strategy.expression, defined_concepts=defined
            )
            assert tokens
            assert strategy.expression
