"""数据库无关的检索能力包与注册表。"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from ...ports import FullTextTool, MetadataTool, SearchTool
from .adapter import QueryAdapter


@dataclass(frozen=True)
class RetrievalSource:
    """必须来自同一来源的一组查询、搜索、全文和元数据能力。"""

    source_id: str
    query_adapter: QueryAdapter
    search_tool: SearchTool | None = None
    full_text_tool: FullTextTool | None = None
    metadata_tool: MetadataTool | None = None

    def __post_init__(self) -> None:
        source_id = self.source_id.strip().lower()
        if not source_id:
            raise ValueError("source_id 不能为空")
        object.__setattr__(self, "source_id", source_id)
        capabilities = {
            "query_adapter": self.query_adapter,
            "search_tool": self.search_tool,
            "full_text_tool": self.full_text_tool,
            "metadata_tool": self.metadata_tool,
        }
        for name, capability in capabilities.items():
            if capability is None:
                continue
            capability_source = getattr(
                capability, "database" if name == "query_adapter" else "source_id", None
            )
            if capability_source != source_id:
                raise ValueError(
                    f"能力来源不一致：{name} 属于 {capability_source!r}，"
                    f"RetrievalSource 为 {source_id!r}"
                )


RetrievalSourceBuilder = Callable[[Mapping[str, Any]], RetrievalSource]


class RetrievalSourceRegistry:
    """通过注册构建器选择来源；新增来源无需修改本类。"""

    def __init__(self) -> None:
        self._builders: dict[str, RetrievalSourceBuilder] = {}

    def register(self, source_id: str, builder: RetrievalSourceBuilder) -> None:
        key = source_id.strip().lower()
        if not key:
            raise ValueError("source_id 不能为空")
        self._builders[key] = builder

    def build(self, source_id: str, config: Mapping[str, Any]) -> RetrievalSource:
        key = source_id.strip().lower()
        try:
            builder = self._builders[key]
        except KeyError as exc:
            supported = ", ".join(sorted(self._builders))
            raise ValueError(f"未知检索来源：{source_id!r}；已注册：{supported}") from exc
        source = builder(config)
        if source.source_id != key:
            raise ValueError(f"构建器 {key!r} 返回了来源 {source.source_id!r}")
        return source
