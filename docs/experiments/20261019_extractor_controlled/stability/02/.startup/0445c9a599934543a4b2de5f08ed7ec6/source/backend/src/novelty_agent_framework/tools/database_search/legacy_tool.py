"""Compatibility wrappers for tools outside the canonical Researcher toolset."""

from __future__ import annotations

import time

from ...schemas import (
    ResearcherToolObservation,
    StructuredRetrievalToolArguments,
    StructuredSourceRetrievalRequest,
    TaskResearchRequest,
)
from .structured_retrieval import StructuredSourceRetrievalTool


class StructuredRetrievalResearcherTool:
    """Compatibility wrapper outside the canonical database_search path.

    LEGACY / UNUSED by the formal Harness. Retained for historical experiments;
    new code must use ``DatabaseSearchTool``.
    """

    name = "structured_source_retrieval"
    description = "在一个已配置结构化来源中检索并保存候选文献。"
    args_schema = StructuredRetrievalToolArguments

    def __init__(self, tools_by_source: dict[str, StructuredSourceRetrievalTool]) -> None:
        self.tools_by_source = dict(tools_by_source)

    async def ainvoke(
        self,
        arguments: StructuredRetrievalToolArguments,
        *,
        scope: TaskResearchRequest,
    ) -> ResearcherToolObservation:
        source_id = arguments.source_id.strip().lower()
        try:
            tool = self.tools_by_source[source_id]
        except KeyError as exc:
            raise ValueError(f"structured source {source_id!r} is unavailable") from exc
        started = time.monotonic()
        bundle = await tool.ainvoke(
            StructuredSourceRetrievalRequest(
                subject_paper_id=scope.subject_paper_id,
                source_id=source_id,
                novelty_point=scope.novelty_point,
                research_task=scope.research_task,
                search_plan=scope.search_plan,
                run_id=scope.run_id,
            )
        )
        return ResearcherToolObservation(
            tool_name=self.name,
            arguments=arguments.model_dump(mode="json"),
            succeeded=True,
            summary=(
                f"发现 {len(bundle.works)} 个作品，保存 {len(bundle.artifacts)} 个制品"
            ),
            payload={"bundle": bundle.model_dump(mode="json")},
            elapsed_ms=int((time.monotonic() - started) * 1000),
        )
