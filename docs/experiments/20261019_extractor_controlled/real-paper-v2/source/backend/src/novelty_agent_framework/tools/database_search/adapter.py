"""把数据库无关的检索计划确定性编译为具体数据库查询。"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass

from ...schemas import SearchConcept, SearchPlan

_TOKEN = re.compile(r"C\d+(?![A-Za-z0-9_])|AND(?![A-Za-z0-9_])|OR(?![A-Za-z0-9_])|[()]")
_CONCEPT_ID = re.compile(r"C\d+")


class QueryAdapterError(ValueError):
    """检索计划无法被可靠地编译为数据库查询。"""


@dataclass(frozen=True)
class CompiledQuery:
    """带完整任务追踪信息的一条数据库查询。"""

    database: str
    task_id: str
    novelty_point_id: str
    strategy_id: str
    level: str
    query: str


class QueryAdapter(ABC):
    """数据库查询适配器的模板方法接口。"""

    database: str

    def compile(self, plan: SearchPlan) -> Sequence[CompiledQuery]:
        """按原顺序编译计划中的全部策略，不执行实际检索。"""

        concepts: dict[str, SearchConcept] = {}
        rendered: dict[str, str] = {}
        for concept in plan.concepts:
            if _CONCEPT_ID.fullmatch(concept.concept_id) is None:
                raise QueryAdapterError(f"非法 Concept ID：{concept.concept_id!r}")
            if concept.concept_id in concepts:
                raise QueryAdapterError(f"重复 Concept ID：{concept.concept_id}")
            concepts[concept.concept_id] = concept
            rendered[concept.concept_id] = self._render_concept(concept)

        compiled: list[CompiledQuery] = []
        for strategy in plan.strategies:
            rendered = {
                concept.concept_id: self._render_concept(
                    concept,
                    use_alias=strategy.use_alias,
                    use_exclude=strategy.use_exclude,
                )
                for concept in plan.concepts
            }
            query = self._compile_expression(strategy.expression, rendered)
            compiled.append(
                CompiledQuery(
                    database=self.database,
                    task_id=plan.task_id,
                    novelty_point_id=plan.novelty_point_id,
                    strategy_id=strategy.strategy_id,
                    level=strategy.level,
                    query=query,
                )
            )
        return compiled

    @abstractmethod
    def _render_concept(
        self,
        concept: SearchConcept,
        *,
        use_alias: bool = False,
        use_exclude: bool = True,
    ) -> str:
        """把单个语义 Concept 编译为数据库专用查询片段。

        use_alias=True 时概念渲染应把 alias 并入 OR 展开（v2 medium/broad）；
        use_exclude=False 时跳过 exclude（放宽链最后兜底）。
        """

    @staticmethod
    def _compile_expression(expression: str, concepts: dict[str, str]) -> str:
        tokens = _tokenize(expression)
        output: list[str] = []
        depth = 0
        expects_operand = True

        for token in tokens:
            if expects_operand:
                if token == "(":
                    depth += 1
                    output.append(token)
                elif _CONCEPT_ID.fullmatch(token):
                    if token not in concepts:
                        raise QueryAdapterError(f"检索表达式引用了未定义 Concept：{token}")
                    output.append(concepts[token])
                    expects_operand = False
                else:
                    raise QueryAdapterError(f"检索表达式在 {token!r} 前缺少 Concept")
                continue

            if token in {"AND", "OR"}:
                output.append(token)
                expects_operand = True
            elif token == ")":
                if depth == 0:
                    raise QueryAdapterError("检索表达式括号不平衡")
                depth -= 1
                output.append(token)
            else:
                raise QueryAdapterError(f"检索表达式在 {token!r} 前缺少 AND/OR")

        if expects_operand:
            raise QueryAdapterError("检索表达式不完整")
        if depth:
            raise QueryAdapterError("检索表达式括号不平衡")
        return " ".join(output).replace("( ", "(").replace(" )", ")")


class AdapterFactory:
    """按数据库名称创建查询适配器。"""

    _adapters: dict[str, type[QueryAdapter]] = {}

    @classmethod
    def create(cls, database: str) -> QueryAdapter:
        normalized = database.strip().lower()
        adapter_type = cls._adapters.get(normalized)
        if adapter_type is None:
            supported = ", ".join(sorted(cls._adapters))
            raise QueryAdapterError(
                f"不支持的检索数据库：{database!r}；当前支持：{supported}"
            )
        return adapter_type()

    @classmethod
    def register(cls, database: str, adapter: type[QueryAdapter]) -> None:
        cls._adapters[database.strip().lower()] = adapter


def compile_search_plan(
    plan: SearchPlan,
    *,
    database: str = "arxiv",
) -> list[CompiledQuery]:
    """通过已注册 Adapter 编译 SearchPlan，不执行查询。"""

    normalized = database.strip().lower()
    # 兼容旧的默认 arXiv API；延迟加载保证通用模块导入不依赖具体实现。
    if normalized == "arxiv" and "arxiv" not in AdapterFactory._adapters:
        from .providers.arxiv import ArxivQueryAdapter

        AdapterFactory.register("arxiv", ArxivQueryAdapter)
    if (
        normalized == "sciencedirect"
        and "sciencedirect" not in AdapterFactory._adapters
    ):
        from .providers.sciencedirect import ScienceDirectQueryAdapter

        AdapterFactory.register("sciencedirect", ScienceDirectQueryAdapter)
    if normalized == "springer" and "springer" not in AdapterFactory._adapters:
        from .providers.springer import SpringerNatureQueryAdapter

        AdapterFactory.register("springer", SpringerNatureQueryAdapter)
    if (
        normalized == "ieee_xplore"
        and "ieee_xplore" not in AdapterFactory._adapters
    ):
        from .providers.ieee_xplore import IEEEXploreQueryAdapter

        AdapterFactory.register("ieee_xplore", IEEEXploreQueryAdapter)
    return list(AdapterFactory.create(database).compile(plan))


def _tokenize(expression: str) -> list[str]:
    tokens: list[str] = []
    position = 0
    while position < len(expression):
        if expression[position].isspace():
            position += 1
            continue
        match = _TOKEN.match(expression, position)
        if match is None:
            fragment = expression[position : position + 16].split(maxsplit=1)[0]
            raise QueryAdapterError(f"检索表达式包含不支持的 token：{fragment!r}")
        tokens.append(match.group())
        position = match.end()
    if not tokens:
        raise QueryAdapterError("检索表达式不能为空")
    return tokens


def _escape_quoted_term(term: str) -> str:
    return term.replace("\\", "\\\\").replace('"', '\\"')
