"""Formal agent-facing database_search tool."""

from __future__ import annotations

import time
from collections import Counter
from collections.abc import Mapping, Sequence
from typing import Any

from ...persistence import ReferenceStore
from ...schemas import (
    DatabaseSearchArguments,
    DatabaseSearchItem,
    DatabaseSearchResult,
    ResearcherToolObservation,
    SearchExecution,
    SearchExecutionStatus,
    StructuredSourceRetrievalRequest,
    TaskResearchRequest,
)
from .structured_retrieval import StructuredSourceRetrievalTool


def _summarize_search_executions(
    executions: Sequence[SearchExecution],
) -> dict[str, int | bool]:
    """Aggregate provider-neutral SearchExecution status facts."""

    counts = Counter(execution.status for execution in executions)
    total = len(executions)
    failed = counts[SearchExecutionStatus.FAILED]
    not_run = counts[SearchExecutionStatus.NOT_RUN]
    usable = (
        counts[SearchExecutionStatus.SUCCEEDED]
        + counts[SearchExecutionStatus.PARTIAL]
    )
    attempted = total - not_run
    all_failed = attempted > 0 and failed == attempted
    return {
        "total": total,
        "succeeded": counts[SearchExecutionStatus.SUCCEEDED],
        "partial": counts[SearchExecutionStatus.PARTIAL],
        "failed": failed,
        "not_run": not_run,
        "requires_human": counts[SearchExecutionStatus.REQUIRES_HUMAN],
        "degraded": failed > 0 and usable > 0,
        "all_failed": all_failed,
        "provider_failed": failed > 0,
        "no_execution": attempted == 0,
        "retrieval_incomplete_budget": any(
            item.status == SearchExecutionStatus.NOT_RUN
            and item.parameters.get("not_run_reason") == "retrieval_incomplete_budget"
            for item in executions
        ),
    }


class DatabaseSearchTool:
    name = "database_search"
    args_schema = DatabaseSearchArguments

    def __init__(
        self,
        tools_by_source: Mapping[str, StructuredSourceRetrievalTool],
        reference_store: ReferenceStore,
    ) -> None:
        normalized = {key.strip().lower(): value for key, value in tools_by_source.items()}
        if not normalized:
            raise ValueError("at least one database source is required")
        for source_id, tool in normalized.items():
            if source_id != tool.source_id:
                raise ValueError(
                    f"database source key {source_id!r} does not match tool {tool.source_id!r}"
                )
            if tool.reference_store is not reference_store:
                raise ValueError("all database retrieval tools must share reference_store")
        self.tools_by_source = normalized
        self.reference_store = reference_store
        available = ", ".join(sorted(normalized))
        self.description = (
            "搜索已配置的结构化文献数据库并保存候选作品；"
            f"source_id 只能使用以下值：{available}。"
            "候选来源本身不是证据；结果包含 artifact_ids 时，下一次工具调用"
            "必须优先使用 reader 读取其中一个 Artifact，不得继续搜索。"
            "同一任务的检索计划固定；重复调用相同 source_id 仅复用完整成功结果（含零命中）。"
            "失败或部分成功不缓存；临时错误按 provider 退避重试，其他错误优先换库。"
            "成功后优先批量读取已有候选；新一轮任务可重新检索。"
            "默认先返回候选摘要。如摘要不足以核验具体特征，可传入"
            "full_text_source_record_ids（至多4个已返回的同库文献句柄）按需获取全文；"
            "此模式不重新检索。获取后必须通过 reader 读取原文，才能作为证据。"
        )

    async def ainvoke(
        self,
        arguments: DatabaseSearchArguments,
        *,
        scope: TaskResearchRequest,
    ) -> ResearcherToolObservation:
        source_id = arguments.source_id
        try:
            retrieval = self.tools_by_source[source_id]
        except KeyError as exc:
            available = ", ".join(sorted(self.tools_by_source))
            raise ValueError(
                f"database source {source_id!r} is unavailable; configured: {available}"
            ) from exc

        started = time.monotonic()
        request = StructuredSourceRetrievalRequest(
                subject_paper_id=scope.subject_paper_id,
                run_id=scope.run_id,
                source_id=source_id,
                novelty_point=scope.novelty_point,
                research_task=scope.research_task,
                search_plan=scope.search_plan,
            )
        acquisition = bool(arguments.full_text_source_record_ids)
        bundle = (await retrieval.acquire_full_texts(request, arguments.full_text_source_record_ids)
                  if acquisition else await retrieval.ainvoke(request))
        artifacts_by_record: dict[str, list[str]] = {}
        for artifact in bundle.artifacts:
            if artifact.source_record_id is not None:
                artifacts_by_record.setdefault(artifact.source_record_id, []).append(
                    artifact.artifact_id
                )
        works = {work.work_id: work for work in bundle.works}
        items: list[DatabaseSearchItem] = []
        for record in bundle.source_records:
            if record.work_id is None or record.work_id not in works:
                continue
            work = works[record.work_id]
            preview = record.abstract[:500] if record.abstract else None
            items.append(
                DatabaseSearchItem(
                    source_record_id=record.source_record_id,
                    work_id=work.work_id,
                    title=work.title,
                    authors=work.authors,
                    publication_year=work.publication_year,
                    source_id=record.source_id,
                    access_status=record.access_status,
                    artifact_ids=sorted(artifacts_by_record.get(record.source_record_id, [])),
                    full_text_artifact_ids=[a.artifact_id for a in bundle.artifacts
                        if a.source_record_id == record.source_record_id and a.role.value == "extracted_text"],
                    abstract_preview=preview,
                )
            )
        execution_summary = _summarize_search_executions(bundle.search_executions)
        if acquisition:
            # No search was attempted: retain real execution history without inventing queries.
            execution_summary["no_execution"] = False
        warnings = list(bundle.warnings)
        if execution_summary["degraded"]:
            warnings.append(
                "database search degraded: "
                f"{execution_summary['failed']}/{execution_summary['total']} "
                "search executions failed"
            )
        result = DatabaseSearchResult(
            source_id=source_id,
            results=items,
            warnings=warnings,
        )
        succeeded = not (
            execution_summary["no_execution"] or execution_summary["all_failed"]
        )
        if execution_summary["no_execution"]:
            summary = "数据库检索执行失败：没有产生 search execution"
            error = "no_search_execution: database search produced no search executions"
        elif execution_summary["all_failed"]:
            total = execution_summary["total"] - execution_summary["not_run"]
            summary = (
                f"数据库检索执行失败：{total}/{total} search executions failed"
            )
            error = f"all {total} search executions failed"
        else:
            summary = f"数据库检索召回 {len(items)} 个候选作品"
            if acquisition:
                summary = f"按需全文获取完成，返回 {len(items)} 篇文献的可用制品；全文不可用时保留摘要"
            error = None
            if execution_summary["degraded"]:
                summary += (
                    "；部分执行失败："
                    f"{execution_summary['failed']}/{execution_summary['total']}"
                )
        return ResearcherToolObservation(
            tool_name=self.name,
            arguments=arguments.model_dump(mode="json"),
            succeeded=succeeded,
            summary=summary,
            payload={
                "research_bundle": bundle.model_dump(mode="json"),
                "execution_issues": [issue.model_dump(mode="json") for issue in bundle.execution_failures],
                "database_search_result": result.model_dump(mode="json"),
                "search_executions": [
                    item.model_dump(mode="json") for item in bundle.search_executions
                ],
                "source_records": [
                    item.model_dump(mode="json") for item in bundle.source_records
                ],
                "artifacts": [item.model_dump(mode="json") for item in bundle.artifacts],
                "execution_summary": execution_summary,
            },
            error=error,
            elapsed_ms=int((time.monotonic() - started) * 1_000),
        )

    def project_model_context(
        self, observation: ResearcherToolObservation
    ) -> dict[str, Any]:
        result = observation.payload["database_search_result"]
        return {
            "succeeded": observation.succeeded,
            "summary": observation.summary,
            "error": observation.error,
            "execution_summary": observation.payload["execution_summary"],
            "execution_issues": observation.payload.get("execution_issues", []),
            "result_marker": ("zero_hits" if observation.succeeded and not result["results"]
                              and not observation.payload["execution_summary"].get("degraded")
                              else "has_hits" if observation.succeeded and result["results"]
                              else "partial" if observation.succeeded else "failed"),
            "retrieval_status": (
                "PROVIDER_FAILED" if observation.payload["execution_summary"].get("provider_failed")
                else "ZERO_RESULT" if observation.succeeded and not result["results"]
                else "HAS_RESULTS" if observation.succeeded else "FAILED"
            ),
            "source_id": result["source_id"],
            "results": result["results"],
            "warnings": result["warnings"],
        }
