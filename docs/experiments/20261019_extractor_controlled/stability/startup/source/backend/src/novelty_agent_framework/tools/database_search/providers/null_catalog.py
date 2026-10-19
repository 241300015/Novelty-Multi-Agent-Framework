"""伴随项目的离线 Null Object 数据源。

null_catalog 只验证注册、配置选择、Adapter/SearchTool 替换和空结果处理；
它不模拟文献数据库，不验证全文或元数据能力，也不用于正式科技查新。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from ....ports import SearchHit, SearchTool
from ....schemas import SearchConcept
from ..adapter import QueryAdapter, QueryAdapterError
from ..retrieval_sources import RetrievalSource


class NullQueryAdapter(QueryAdapter):
    """校验正式 SearchPlan，并编译为确定性的中性空查询语法。"""

    database = "null_catalog"

    def _render_concept(
        self,
        concept: SearchConcept,
        *,
        use_alias: bool = False,
        use_exclude: bool = True,
    ) -> str:
        terms: list[str] = []
        for raw_term in concept.terms:
            term = " ".join(raw_term.split())
            if not term:
                raise QueryAdapterError(f"Concept {concept.concept_id} 包含空 term")
            if term not in terms:
                terms.append(term)
        # 查询不执行，但保留 Concept 内容让编译结果可审计、可确定断言。
        return f"CONCEPT[{concept.concept_id}:{'|'.join(terms)}]"

    @staticmethod
    def _compile_expression(expression: str, concepts: dict[str, str]) -> str:
        validated = QueryAdapter._compile_expression(expression, concepts)
        return f"NULL_QUERY({validated})"


class NullSearchTool(SearchTool):
    """永远返回空结果，不访问网络、文件或其他数据源。"""

    source_id = "null_catalog"

    def search(self, query: str, *, limit: int = 10) -> Sequence[SearchHit]:
        return ()


def build_null_catalog_source(config: Mapping[str, Any]) -> RetrievalSource:
    """构建与 arXiv 处于相同注入位置的空能力包。"""

    return RetrievalSource(
        source_id="null_catalog",
        query_adapter=NullQueryAdapter(),
        search_tool=NullSearchTool(),
        full_text_tool=None,
        metadata_tool=None,
    )
