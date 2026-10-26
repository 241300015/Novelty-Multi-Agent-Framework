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
    ArtifactRole,
    FullTextAcquisitionRequest,
    ResearcherToolObservation,
    SearchExecution,
    SearchExecutionStatus,
    StructuredSourceRetrievalRequest,
    TaskResearchRequest,
)
from .structured_retrieval import StructuredSourceRetrievalTool
from ...core import local_correction as local
from ...schemas.failures import FailureEvent, FailureCode, RecoveryAction, failure_family



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

    async def ainvoke(self, arguments: DatabaseSearchArguments, *, scope: TaskResearchRequest):
        observation = await self._invoke_once(arguments, scope=scope)
        # Transient errors belong to task-end retry. Acquisition handles cannot
        # be transplanted into another source. Frozen recovery directives win.
        if arguments.full_text_source_record_ids or scope.recovery is not None or local.retry_queries.get() is not None:
            return observation
        rows = observation.payload.get("search_executions", [])
        failures = [FailureEvent.model_validate(row["failure"]) for row in rows if row.get("failure")]
        switchable = {FailureCode.PROVIDER_AUTHENTICATION, FailureCode.PROVIDER_AUTHORIZATION,
                      FailureCode.PROVIDER_PROTOCOL, FailureCode.PROVIDER_RESOURCE_MISSING}
        if observation.succeeded or not failures or not any(failure_family(f.code) in switchable for f in failures):
            return observation
        failure = next(f for f in failures if failure_family(f.code) in switchable)
        record = local.detect(scope, failure.code, "DatabaseSearchTool.ainvoke", failure=failure,
            message="Original source failed", inputs=arguments.model_dump(mode="json"),
            targets={"source_ids": [arguments.source_id]})
        choices = [source for source in self.tools_by_source if source != arguments.source_id]
        session = local.current_session.get()
        key = "source:" + scope.search_plan.model_dump_json()
        if not choices or (session is not None and not session.once(key)):
            record = local.stop(record, "No authorized alternative source or source change already attempted")
            return observation.model_copy(update={"payload": {**observation.payload,
                "correction": record.model_dump(mode="json")}})
        alternate = choices[0]
        try:
            local.charge_tool("database_search")
            record = local.begin(record, RecoveryAction.CHANGE_PROVIDER)
            changed_arguments = arguments.model_copy(update={"source_id": alternate})
            changed = await local.audited_call(self.name, changed_arguments.model_dump(mode="json"),
                lambda: self._invoke_once(changed_arguments, scope=scope))
            record = local.finish(record, changed.succeeded, "DatabaseSearchTool.execution_summary",
                result={"from_source": arguments.source_id, "to_source": alternate,
                        "search_executions": changed.payload.get("search_executions", [])},
                reason="Alternative source failed")
        except Exception as exc:
            record = local.stop(record, "Source change stopped: " + type(exc).__name__)
            return observation.model_copy(update={"payload": {**observation.payload,
                "correction": record.model_dump(mode="json")}})
        return changed.model_copy(update={"payload": {**changed.payload,
            "source_change": {"from": arguments.source_id, "to": alternate},
            "prior_research_bundles": [observation.payload["research_bundle"]],
            "search_executions": [*rows, *changed.payload.get("search_executions", [])],
            "execution_issues": [*observation.payload.get("execution_issues", []),
                                 *changed.payload.get("execution_issues", [])],
            "correction": record.model_dump(mode="json")}})

    async def acquire_full_texts(
        self,
        arguments: DatabaseSearchArguments,
        *,
        scope: FullTextAcquisitionRequest,
    ) -> ResearcherToolObservation:
        """Acquire verified full text without accepting or constructing a SearchPlan."""
        if not arguments.full_text_source_record_ids:
            raise ValueError("full-text acquisition requires source record ids")
        if arguments.source_id != scope.source_id:
            raise ValueError("database source must match the acquisition scope")
        return await self._invoke_once(arguments, scope=scope)

    async def _invoke_once(
        self,
        arguments: DatabaseSearchArguments,
        *,
        scope: TaskResearchRequest | FullTextAcquisitionRequest,
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
        acquisition = bool(arguments.full_text_source_record_ids)
        if acquisition:
            request = FullTextAcquisitionRequest(
                subject_paper_id=scope.subject_paper_id,
                run_id=scope.run_id,
                source_id=source_id,
                novelty_point=scope.novelty_point,
                research_task=scope.research_task,
            )
            previously_verified = self._verified_full_text_records(
                scope.subject_paper_id, arguments.full_text_source_record_ids
            )
            bundle = await retrieval.acquire_full_texts(
                request, arguments.full_text_source_record_ids
            )
        else:
            if not isinstance(scope, TaskResearchRequest):
                raise ValueError("database discovery requires a TaskResearchRequest")
            request = StructuredSourceRetrievalRequest(
                subject_paper_id=scope.subject_paper_id,
                run_id=scope.run_id,
                source_id=source_id,
                novelty_point=scope.novelty_point,
                research_task=scope.research_task,
                search_plan=scope.search_plan,
            )
            previously_verified = set()
            bundle = await retrieval.ainvoke(request)
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
        acquisition_results: list[dict[str, Any]] = []
        if acquisition:
            verified = self._verified_bundle_full_texts(
                scope.subject_paper_id, bundle.artifacts
            )
            failures_by_record: dict[str, list[FailureEvent]] = {}
            for failure in bundle.execution_failures:
                record_id = failure.scope.source_record_id
                if record_id:
                    failures_by_record.setdefault(record_id, []).append(failure)
            for record_id in dict.fromkeys(arguments.full_text_source_record_ids):
                artifact_ids = sorted(verified.get(record_id, []))
                failures = failures_by_record.get(record_id, [])
                if record_id in previously_verified and artifact_ids:
                    status = "reused_verified"
                elif artifact_ids:
                    status = "acquired"
                elif any(f.code == FailureCode.FULLTEXT_UNSUPPORTED for f in failures):
                    status = "unsupported"
                elif any(f.code == FailureCode.FULLTEXT_EMPTY for f in failures):
                    status = "empty"
                else:
                    status = "failed"
                acquisition_results.append({
                    "source_record_id": record_id,
                    "status": status,
                    "full_text_artifact_ids": artifact_ids,
                    "failure_event_ids": [item.event_id for item in failures],
                    "failure_codes": [item.code.value for item in failures],
                })
            completed = sum(
                row["status"] in {"acquired", "reused_verified"}
                for row in acquisition_results
            )
            failed = len(acquisition_results) - completed
            execution_summary = {
                "total": len(acquisition_results),
                "succeeded": completed,
                "partial": completed > 0 and failed > 0,
                "failed": failed,
                "not_run": 0,
                "requires_human": 0,
                "degraded": completed > 0 and failed > 0,
                "all_failed": completed == 0,
                "provider_failed": any(
                    item.code not in {
                        FailureCode.FULLTEXT_UNSUPPORTED,
                        FailureCode.FULLTEXT_EMPTY,
                    }
                    for item in bundle.execution_failures
                ),
                "no_execution": False,
                "retrieval_incomplete_budget": False,
                "operation": "full_text_acquisition",
            }
        else:
            execution_summary = _summarize_search_executions(bundle.search_executions)
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
        succeeded = not (execution_summary["no_execution"] or execution_summary["all_failed"])
        if execution_summary["no_execution"]:
            summary = "数据库检索执行失败：没有产生 search execution"
            error = "no_search_execution: database search produced no search executions"
        elif execution_summary["all_failed"]:
            total = execution_summary["total"] - execution_summary["not_run"]
            if acquisition:
                summary = f"按需全文获取失败：{total}/{total} 条记录均无经校验全文"
                error = f"all {total} full-text acquisitions failed"
            else:
                summary = (
                    f"数据库检索执行失败：{total}/{total} search executions failed"
                )
                error = f"all {total} search executions failed"
        else:
            if acquisition:
                summary = (
                    f"按需全文获取：{execution_summary['succeeded']}/"
                    f"{execution_summary['total']} 条记录取得经校验的全文制品"
                )
            else:
                summary = f"数据库检索召回 {len(items)} 个候选作品"
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
                **({"failure_code": "coverage.not_executed" if execution_summary["no_execution"]
                    else "provider.unknown"} if not succeeded else {}),
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
                **({"acquisition_results": acquisition_results} if acquisition else {}),
            },
            error=error,
            elapsed_ms=int((time.monotonic() - started) * 1_000),
        )

    def _verified_full_text_records(
        self, paper_id: str, record_ids: Sequence[str]
    ) -> set[str]:
        wanted = set(record_ids)
        manifest = self.reference_store.load_manifest(paper_id)
        verified = set()
        for artifact in manifest.artifacts:
            if artifact.source_record_id not in wanted or artifact.role != ArtifactRole.EXTRACTED_TEXT:
                continue
            try:
                self.reference_store.verify_artifact_file(paper_id, artifact.artifact_id)
            except (OSError, ValueError):
                continue
            verified.add(artifact.source_record_id)
        return verified

    def _verified_bundle_full_texts(self, paper_id: str, artifacts) -> dict[str, list[str]]:
        verified: dict[str, list[str]] = {}
        for artifact in artifacts:
            if artifact.source_record_id is None or artifact.role != ArtifactRole.EXTRACTED_TEXT:
                continue
            try:
                self.reference_store.verify_artifact_file(paper_id, artifact.artifact_id)
            except (OSError, ValueError):
                continue
            verified.setdefault(artifact.source_record_id, []).append(artifact.artifact_id)
        return verified

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
            **({"acquisition_results": observation.payload["acquisition_results"]}
               if "acquisition_results" in observation.payload else {}),
        }
