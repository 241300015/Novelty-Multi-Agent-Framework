"""确定性补全器：把最小模型契约 SearchPlanDraft（v2）编译为运行时 SearchPlan。

设计边界（见 docs/search-planner-optimization-v2.md）：
- LLM 只生成语义载荷（concepts.role/terms/alias/exclude/importance +
  strategies.level/focus_concepts）；
- 概念编号、策略编号、任务绑定、布尔表达式、level 语义、name/description
  全部由本模块代码生成——LLM 不输出任何布尔表达式；
- 表达式按模板组装：strict = anchor AND 特征（仅 terms）；
  medium = anchor AND 较少特征（terms+alias）；broad = anchor（terms+alias）；
- 语义校验失败返回结构化 DraftIssue 列表（code/detail/fix），供重试 prompt 使用。
- 本模块不调用 LLM、不访问网络，是可单测的纯函数。
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from ..core.search_plan_expression import (
    SearchPlanExpressionError,
    parse_search_plan_expression,
)
from ..schemas import ResearchTask, SearchConcept, SearchPlan, SearchStrategy
from ..schemas.search_plan_draft import SearchPlanDraft
from ..schemas.failures import FailureCode

CONCEPT_ID_PATTERN = re.compile(r"C\d+")
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
_ASCII_RE = re.compile(r"[A-Za-z]")
_WORD_RE = re.compile(r"[a-z0-9\u4e00-\u9fff]+", re.IGNORECASE)

STRICT_CAP = 2
MEDIUM_CAP = 2
BROAD_CAP = 4


@dataclass(frozen=True)
class SemanticLimits:
    """语义校验的数量预算（配置可注入；默认值与 prompt 文本严格一致）。"""

    max_concepts: int = 6
    max_terms_per_concept: int = 5
    max_alias_per_concept: int = 4
    max_exclude_per_concept: int = 3
    max_term_words: int = 8
    require_escape: bool = False


GENERIC_TERMS = frozenset({
    "efficient", "robust", "adaptive", "invariant", "unsupervised", "supervised",
    "general", "novel", "fast", "scalable", "effective", "improved", "enhanced",
    "learning", "optimization", "optimisation", "framework", "approach",
    "method", "model", "system", "analysis", "evaluation",
    "基于", "方法", "模型", "系统", "优化", "学习", "高效", "鲁棒", "自适应", "新型",
})


class SearchPlanCompilationError(ValueError):
    """SearchPlanDraft 无法被可靠地补全为运行时 SearchPlan。

    issues 携带结构化错误列表（code/detail/fix），供 SearchPlannerAgent
    组装重试 prompt。
    """

    def __init__(self, message: str, issues: list["DraftIssue"] | None = None) -> None:
        super().__init__(message)
        self.issues = issues or []


@dataclass(frozen=True)
class DraftIssue:
    """一条结构化语义错误。"""

    code: str
    detail: str
    fix: str
    paths: tuple[tuple[str | int, ...], ...] = ()

    @property
    def failure_code(self) -> FailureCode:
        """Keep legacy local suffixes readable while using the central catalog."""
        return FailureCode(f"planner.draft.{self.code}")

    def format(self) -> str:
        return f"[{self.code}] {self.detail} —— 修复：{self.fix}"


def validate_draft_semantics(
    draft: SearchPlanDraft,
    *,
    task: ResearchTask,
    limits: SemanticLimits | None = None,
) -> list[DraftIssue]:
    """校验 draft 的语义质量，返回结构化错误列表（空列表 = 通过）。"""

    limits = limits or SemanticLimits()

    issues: list[DraftIssue] = []

    if not draft.concepts:
        issues.append(DraftIssue("empty_concepts", "concepts 为空", "至少给出 1 个概念", (("concepts",),)))
    if not draft.strategies:
        issues.append(DraftIssue("empty_strategies", "strategies 为空", "至少给出 1 条策略", (("strategies",),)))

    concept_ids = {f"C{i + 1}" for i in range(len(draft.concepts))}
    has_cjk = any(_CJK_RE.search(t) for c in draft.concepts for t in c.terms)
    has_ascii = any(_ASCII_RE.search(t) for c in draft.concepts for t in c.terms)
    roles = {c.role for c in draft.concepts}

    if len(draft.concepts) > limits.max_concepts:
        issues.append(
            DraftIssue(
                "too_many_concepts",
                f"共 {len(draft.concepts)} 个概念（上限 {limits.max_concepts}）",
                f"合并或删除冗余概念，最多保留 {limits.max_concepts} 个",
                (("concepts",),),
            )
        )

    for index, concept in enumerate(draft.concepts):
        cid = f"C{index + 1}"
        terms = [t.strip() for t in concept.terms if t.strip()]
        if not terms:
            issues.append(
                DraftIssue("blank_term", f"{cid} 的词项全为空", "给出非空且符合本次词数上限的名词短语", (("concepts", index, "terms"),))
            )
            continue
        if len(terms) > limits.max_terms_per_concept:
            issues.append(
                DraftIssue(
                    "too_many_terms",
                    f"{cid} 有 {len(terms)} 个词项（上限 {limits.max_terms_per_concept}）",
                    f"每个概念最多保留 {limits.max_terms_per_concept} 个词项",
                    (("concepts", index, "terms"),),
                )
            )
        for term in terms:
            words = _WORD_RE.findall(term.casefold())
            if len(words) > limits.max_term_words:
                issues.append(
                    DraftIssue(
                        "sentence_like_term",
                        f"{cid} 的词项「{term[:40]}」超过 {limits.max_term_words} 词，疑似完整句子",
                        f"压缩为不超过 {limits.max_term_words} 词的名词短语",
                        (("concepts", index, "terms"),),
                    )
                )
            elif words and all(w in GENERIC_TERMS for w in words):
                issues.append(
                    DraftIssue(
                        "generic_term",
                        f"{cid} 的词项「{term[:40]}」全部是通用修饰词，没有具体对象",
                        "换成领域拥有的具体对象名词短语（词汇所有权测试 + 具体对象测试）",
                        (("concepts", index, "terms"),),
                    )
                )
        if len(concept.alias) > limits.max_alias_per_concept:
            issues.append(
                DraftIssue(
                    "too_many_alias",
                    f"{cid} 有 {len(concept.alias)} 个别名（上限 {limits.max_alias_per_concept}）",
                    f"别名最多保留 {limits.max_alias_per_concept} 个",
                    (("concepts", index, "alias"),),
                )
            )
        if len(concept.exclude) > limits.max_exclude_per_concept:
            issues.append(
                DraftIssue(
                    "too_many_exclude",
                    f"{cid} 有 {len(concept.exclude)} 个排除词（上限 {limits.max_exclude_per_concept}）",
                    f"exclude 只放确定噪声，最多 {limits.max_exclude_per_concept} 个",
                    (("concepts", index, "exclude"),),
                )
            )

    positives = [
        (f"C{index + 1}", value, _concept_tokens(value))
        for index, concept in enumerate(draft.concepts)
        for value in [*concept.terms, *concept.alias]
        if _concept_tokens(value)
    ]
    for index, concept in enumerate(draft.concepts):
        seen_excludes: set[str] = set()
        for exclude in concept.exclude:
            tokens = _concept_tokens(exclude)
            if not tokens or " ".join(tokens) in seen_excludes:
                continue
            seen_excludes.add(" ".join(tokens))
            for concept_id, positive, positive_tokens in positives:
                if _exclude_conflicts(tokens, positive_tokens):
                    issues.append(DraftIssue(
                        "exclude_conflicts_positive_concept",
                        f"exclude {exclude!r} conflicts with positive concept {concept_id} {positive!r}",
                        "remove the conflicting exclude or replace it with a true noise term",
                        (("concepts", index, "exclude"),),
                    ))
                    break

    language = (task.language or "").strip().lower()
    if language == "en" and not has_ascii:
        issues.append(
            DraftIssue("missing_en_term", "en 任务没有任何英文词项", "至少一个概念使用英文术语（arXiv 检索需要）",
                       tuple(("concepts", i, "terms") for i in range(len(draft.concepts))))
        )

    if task.task_type == "literature_search" and limits.require_escape:
        if "escape" not in roles:
            issues.append(
                DraftIssue(
                    "missing_escape",
                    "literature_search 缺少 escape 角色概念",
                    "添加 escape 概念：用『这篇论文的贡献已被实现时，那篇论文会使用的解法词汇』命名",
                    (("concepts",),),
                )
            )
        if not roles & {"object", "method"}:
            issues.append(
                DraftIssue("missing_object_method", "缺少 object/method 角色概念", "至少一个概念标记为 object 或 method",
                           tuple(("concepts", i, "role") for i in range(len(draft.concepts))))
            )

    levels = [s.level for s in draft.strategies]
    if task.task_type == "literature_search":
        if len(levels) != 3 or set(levels) != {"strict", "medium", "broad"}:
            issues.append(
                DraftIssue(
                    "strategy_levels",
                    f"literature_search 必须恰好三条策略且 level 为 strict/medium/broad，实际 {levels}",
                    "输出三条策略：strict、medium、broad 各一条",
                    (("strategies",),),
                )
            )
    else:
        if len(levels) != len(set(levels)):
            issues.append(
                DraftIssue(
                    "duplicate_level",
                    f"补检任务的策略 level 重复：{levels}",
                    "每条策略使用不同 level（strict/medium/broad 中选 1~3 条）",
                    (("strategies",),),
                )
            )

    for index, strategy in enumerate(draft.strategies):
        for ref in strategy.focus_concepts:
            if not CONCEPT_ID_PATTERN.fullmatch(ref) or ref not in concept_ids:
                issues.append(
                    DraftIssue(
                        "undefined_concept",
                        f"策略 {index + 1} 的 focus_concepts 引用了未定义概念 {ref}",
                        f"只引用已定义编号 C1..C{len(draft.concepts)}，或留空交给模板",
                        (("strategies", index, "focus_concepts"),),
                    )
                )

    return issues


def _concept_tokens(value: str) -> tuple[str, ...]:
    return tuple(_WORD_RE.findall(unicodedata.normalize("NFKC", value).casefold()))


def _exclude_conflicts(exclude: tuple[str, ...], positive: tuple[str, ...]) -> bool:
    if exclude == positive:
        return True
    # A single meaningful word in a positive phrase must not be negated. For a
    # longer phrase, require a contiguous match to avoid accidental overlap.
    if len(exclude) == 1:
        return len(exclude[0]) >= 3 and exclude[0] in positive
    return any(positive[index:index + len(exclude)] == exclude
               for index in range(len(positive) - len(exclude) + 1))


def build_runtime_plan(
    draft: SearchPlanDraft,
    *,
    task: ResearchTask,
    limits: SemanticLimits | None = None,
) -> SearchPlan:
    """把最小模型契约补全为完整运行时 SearchPlan（纯代码，无 LLM）。"""

    issues = validate_draft_semantics(draft, task=task, limits=limits)
    if issues:
        raise SearchPlanCompilationError(
            "SearchPlanDraft 语义校验失败：" + "；".join(issue.format() for issue in issues),
            issues=issues,
        )

    concepts: list[SearchConcept] = []
    for index, concept in enumerate(draft.concepts):
        terms = [term.strip() for term in concept.terms if term.strip()]
        concepts.append(
            SearchConcept(
                concept_id=f"C{index + 1}",
                name=terms[0],
                terms=terms,
                role=concept.role,
                alias=list(concept.alias),
                exclude=list(concept.exclude),
                importance=concept.importance,
            )
        )

    by_id = {concept.concept_id: concept for concept in concepts}
    anchor_id = _anchor_id(concepts)
    level_ids = _level_pools(draft, concepts, anchor_id)
    strategies: list[SearchStrategy] = []
    for index, strategy in enumerate(draft.strategies):
        ids = level_ids[strategy.level]
        expression = " AND ".join(ids)
        strategies.append(
            SearchStrategy(
                strategy_id=f"S{index + 1}",
                level=strategy.level,
                expression=expression,
                description=_describe(expression, by_id),
                use_alias=strategy.level != "strict",
            )
        )

    if task.task_type == "literature_search":
        _assert_anchor_preserved(strategies, anchor_id)

    _validate_compiled_expressions(strategies, concepts)

    return SearchPlan(
        task_id=task.task_id,
        novelty_point_id=task.novelty_point_id,
        concepts=concepts,
        strategies=strategies,
        protected_concept_ids=[anchor_id] if task.task_type == "literature_search" else [],
    )


def _validate_compiled_expressions(
    strategies: list[SearchStrategy],
    concepts: list[SearchConcept],
) -> None:
    """防御性守卫：模板/覆盖生成的表达式必须符合共享 DSL 语法。

    表达式由本模块确定性生成，正常情况下必然合法；此校验防止未来改动
    破坏表达式模板（如引入非法 token、未定义概念引用或括号失衡）。
    """

    defined_concepts = {concept.concept_id for concept in concepts}
    for strategy in strategies:
        try:
            parse_search_plan_expression(
                strategy.expression,
                defined_concepts=defined_concepts,
            )
        except SearchPlanExpressionError as exc:
            raise SearchPlanCompilationError(
                "模板生成的表达式不符合共享 DSL 语法："
                f"{strategy.strategy_id} {strategy.expression!r}：{exc}"
            ) from exc


def _pool_for(
    strategy: object,
    concepts: list[SearchConcept],
) -> list[str]:
    """选择策略引用的概念编号。focus_concepts 优先；否则按模板选池。"""

    if getattr(strategy, "focus_concepts", None):
        return [ref for ref in strategy.focus_concepts]

    ranked = sorted(
        enumerate(concepts),
        key=lambda item: (-item[1].importance, item[0]),
    )
    level = strategy.level
    if level == "strict":
        pool = [item for item in ranked if item[1].importance >= 3] or ranked[:1]
        cap = STRICT_CAP
    elif level == "medium":
        pool = [item for item in ranked if item[1].importance >= 2] or ranked[:2]
        cap = MEDIUM_CAP
    else:
        pool = ranked
        cap = BROAD_CAP
    return [f"C{index + 1}" for index, _ in pool[:cap]]


def _anchor_id(concepts: list[SearchConcept]) -> str:
    eligible = [concept for concept in concepts if concept.role in {"object", "method"}]
    if not eligible:
        eligible = [concept for concept in concepts if concept.role != "escape"] or concepts
    ranked = sorted(enumerate(eligible), key=lambda item: (
        -int(len(_concept_tokens(item[1].terms[0])) >= 2),
        -item[1].importance,
        -int(item[1].role == "object"),
        item[0],
    ))
    return ranked[0][1].concept_id


def _level_pools(draft: SearchPlanDraft, concepts: list[SearchConcept],
                 anchor_id: str) -> dict[str, list[str]]:
    by_level = {strategy.level: strategy for strategy in draft.strategies}
    if len(by_level) == 1:
        # A single supplemental strategy is not a relaxation chain.
        return {level: _pool_for(strategy, concepts) for level, strategy in by_level.items()}

    strict_source = by_level.get("strict") or by_level.get("medium")
    strict_ids = list(dict.fromkeys([anchor_id, *_pool_for(strict_source, concepts)]))
    medium_source = by_level.get("medium")
    medium_focus = _pool_for(medium_source, concepts) if medium_source else strict_ids
    strict_features = [concept_id for concept_id in strict_ids if concept_id != anchor_id]
    medium_features = [concept_id for concept_id in medium_focus if concept_id in strict_features]
    if not medium_features:
        medium_features = strict_features[:1]
    pools = {"strict": strict_ids, "medium": [anchor_id, *medium_features[:1]],
             "broad": [anchor_id]}
    return {level: pools[level] for level in by_level}


def _assert_anchor_preserved(strategies: list[SearchStrategy], anchor_id: str) -> None:
    by_level = {strategy.level: strategy for strategy in strategies}
    pools = {level: set(CONCEPT_ID_PATTERN.findall(strategy.expression))
             for level, strategy in by_level.items()}
    if any(anchor_id not in ids for ids in pools.values()):
        raise SearchPlanCompilationError("检索策略丢失核心主题 anchor")
    if not (pools["broad"] <= pools["medium"] <= pools["strict"]):
        raise SearchPlanCompilationError("strict/medium/broad 的主题限制未逐级放宽")


def _describe(expression: str, by_id: dict[str, SearchConcept]) -> str:
    """把表达式中的概念 ID 替换为概念名，生成可读描述。"""

    return re.sub(
        CONCEPT_ID_PATTERN,
        lambda match: by_id[match.group(0)].name,
        expression,
    )

@dataclass(frozen=True)
class FallbackVariant:
    """一条零命中放宽变体（编译期预生成，纯数据）。"""

    variant_id: str
    base_strategy_id: str
    level: str
    expression: str
    use_alias: bool
    use_exclude: bool = True
    drop_reason: str = ""


def build_fallback_chain(
    plan: SearchPlan, *, preserve_protected: bool = True,
) -> list[FallbackVariant]:
    """为运行时 SearchPlan 生成有序放宽链（纯函数，可单测）。

    每个基础策略至多一个放宽变体：strict/medium 丢弃最低 importance 概念，
    broad 去除 exclude 词（最后兜底）；命中即停由执行端负责。
    链长上限 = 策略数 × 2（≤6）。
    """

    by_id = {concept.concept_id: concept for concept in plan.concepts}
    protected = set(plan.protected_concept_ids) if preserve_protected else set()
    if protected - by_id.keys():
        raise SearchPlanCompilationError("保护概念引用了未定义概念")
    chain: list[FallbackVariant] = []
    for strategy in plan.strategies:
        ids = CONCEPT_ID_PATTERN.findall(strategy.expression)
        chain.append(
            FallbackVariant(
                variant_id=strategy.strategy_id,
                base_strategy_id=strategy.strategy_id,
                level=strategy.level,
                expression=strategy.expression,
                use_alias=strategy.use_alias,
                use_exclude=strategy.use_exclude,
            )
        )
        if strategy.level == "broad":
            chain.append(
                FallbackVariant(
                    variant_id=f"{strategy.strategy_id}-fb1",
                    base_strategy_id=strategy.strategy_id,
                    level=strategy.level,
                    expression=strategy.expression,
                    use_alias=strategy.use_alias,
                    use_exclude=False,
                    drop_reason="移除 exclude 排除词（最后兜底）",
                )
            )
        elif len(ids) > 1 and (not preserve_protected or
                               _required_concepts(strategy.expression, set(by_id)) >= protected):
            # Dropping a term is only defined for a flat conjunction. Rejoining
            # tokens from an OR tree would silently change its meaning.
            if (preserve_protected and not re.fullmatch(
                    r"C\d+(?:\s+AND\s+C\d+)+", strategy.expression.strip())):
                continue
            removable = [cid for cid in ids if cid not in protected]
            if not removable:
                continue
            dropped = _lowest_importance_concept(removable, by_id)
            rest = [cid for cid in ids if cid != dropped]
            joiner = " AND "
            chain.append(
                FallbackVariant(
                    variant_id=f"{strategy.strategy_id}-fb1",
                    base_strategy_id=strategy.strategy_id,
                    level=strategy.level,
                    expression=joiner.join(rest),
                    use_alias=strategy.use_alias,
                    use_exclude=strategy.use_exclude,
                    drop_reason=f"丢弃概念 {dropped}（{by_id[dropped].name}，importance={by_id[dropped].importance}）",
                )
            )
    return chain


def _required_concepts(expression: str, defined: set[str]) -> set[str]:
    """Return concepts required by every satisfying branch of the existing DSL."""
    tokens = parse_search_plan_expression(expression, defined_concepts=defined)
    position = 0

    def atom() -> set[str]:
        nonlocal position
        token = tokens[position]
        position += 1
        if token == "(":
            result = disjunction()
            position += 1  # closing parenthesis was validated by the parser
            return result
        return {token}

    def conjunction() -> set[str]:
        nonlocal position
        result = atom()
        while position < len(tokens) and tokens[position] == "AND":
            position += 1
            result |= atom()
        return result

    def disjunction() -> set[str]:
        nonlocal position
        result = conjunction()
        while position < len(tokens) and tokens[position] == "OR":
            position += 1
            result &= conjunction()
        return result

    return disjunction()


def _lowest_importance_concept(
    ids: list[str],
    by_id: dict[str, SearchConcept],
) -> str:
    """按 (importance 升序, 概念序号升序) 选择最低优先概念。"""

    def key(cid: str) -> tuple[int, int]:
        concept = by_id[cid]
        suffix = cid[1:]
        return (concept.importance, int(suffix) if suffix.isdigit() else 0)

    return min(ids, key=key)
