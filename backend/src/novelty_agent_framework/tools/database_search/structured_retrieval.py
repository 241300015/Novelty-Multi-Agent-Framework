"""单任务级结构化来源检索、读取与参考文献入库工具。"""

from __future__ import annotations

from ...core.failure_classification import provider_failure
from ...schemas.failures import FailureScope, FailureCode, make_failure

import asyncio
import hashlib
import inspect
import re
import weakref
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any, TypeVar, cast

from ...persistence import ReferenceStore, merge_record, merge_work
from ...ports import FullText, SearchHit, SearchPlanner
from ...schemas import (
    AccessStatus,
    Artifact,
    ArtifactRole,
    ContentExtent,
    ExternalIdentifier,
    FullTextAcquisitionRequest,
    ResearchBundle,
    SearchExecution,
    SearchExecutionStatus,
    SearchPlan,
    SearchResultRef,
    SourceKind,
    SourceRecord,
    StructuredSourceRetrievalRequest,
    Work,
    WorkType,
    SearchStrategy,
)
from .adapter import CompiledQuery
from ...agents.search_plan_compiler import FallbackVariant, build_fallback_chain
from ...core.runtime_artifacts import (ProviderPhysicalBudgetExceeded,
                                       current_runtime_artifacts)
from .retrieval_sources import RetrievalSource

T = TypeVar("T")


class ReplayMissError(LookupError):
    """An offline response snapshot has no exact match for this request."""


async def _invoke_provider(
    func: Callable[..., T | Awaitable[T]],
    /,
    *args: Any,
    **kwargs: Any,
) -> T:
    """Invoke provider capabilities without running synchronous I/O on the loop."""

    if inspect.iscoroutinefunction(func):
        return await cast(Awaitable[T], func(*args, **kwargs))
    value = await asyncio.to_thread(func, *args, **kwargs)
    if inspect.isawaitable(value):
        return await cast(Awaitable[T], value)
    return value


@dataclass
class _PendingExecution:
    execution_id: str
    query: CompiledQuery
    started_at: datetime
    completed_at: datetime
    hits: list[SearchHit]
    variant: FallbackVariant | None = None


class StructuredRetrievalAdapter:
    """把现有端口对象映射为稳定的参考文献数据模型。"""

    @staticmethod
    def stable_id(prefix: str, *parts: str) -> str:
        payload = "\x1f".join(parts).encode("utf-8")
        return f"{prefix}_{hashlib.sha256(payload).hexdigest()[:24]}"

    def adapt_hit(
        self, hit: SearchHit, source_id: str, observed_at: datetime
    ) -> tuple[Work, SourceRecord, list[str]]:
        external_id = (hit.external_id or hit.document_id).strip()
        warnings: list[str] = []
        identifiers: list[ExternalIdentifier] = []
        if (
            source_id == "arxiv"
            and hit.document_id
            and external_id == hit.document_id
        ):
            warnings.append(
                f"arxiv record {hit.document_id} has no observed version; v1 was not inferred"
            )
        if hit.doi:
            identifiers.append(ExternalIdentifier(namespace="doi", value=hit.doi))
            work_key = f"doi:{_normalize_doi(hit.doi)}"
        elif source_id == "arxiv" and hit.document_id:
            identifiers.append(
                ExternalIdentifier(namespace="arxiv", value=hit.document_id)
            )
            work_key = f"arxiv:{hit.document_id}"
        elif external_id:
            work_key = f"{source_id}:{external_id}"
        else:
            fingerprint = "|".join(
                [
                    _normalize_text(hit.title),
                    "|".join(_normalize_text(author) for author in hit.authors),
                    str(hit.year or ""),
                ]
            )
            work_key = f"fingerprint:{fingerprint}"
            warnings.append(
                f"candidate {hit.title!r} uses a title/author/year fallback fingerprint"
            )
        work_id = self.stable_id("wrk", work_key)
        record_id = self.stable_id("src", source_id, external_id or work_key)
        work = Work(
            work_id=work_id,
            work_type=WorkType.ARTICLE,
            title=hit.title,
            authors=list(hit.authors),
            publication_year=hit.year,
            identifiers=identifiers,
        )
        record = SourceRecord(
            source_record_id=record_id,
            work_id=work_id,
            source_id=source_id,
            source_kind=SourceKind.STRUCTURED_DATABASE,
            external_id=external_id or None,
            title=hit.title,
            authors=list(hit.authors),
            abstract=hit.abstract or None,
            publication_year=hit.year,
            identifiers=list(identifiers),
            landing_url=hit.url,
            full_text_url=hit.full_text_url,
            access_status=(
                AccessStatus.METADATA_ONLY
                if hit.abstract
                else AccessStatus.DISCOVERED
            ),
            raw_metadata=hit.raw_metadata,
            observed_at=observed_at,
            provenance={
                "adapter": "structured_retrieval",
                "document_id": hit.document_id,
                "identity_basis": work_key.split(":", 1)[0],
            },
        )
        return work, record, warnings


class StructuredSourceRetrievalTool:
    """Injected SearchPlan → RetrievalSource → Metadata/FullText → ResearchBundle。"""

    name = "structured_source_retrieval"

    def __init__(
        self,
        *,
        search_planner: SearchPlanner | None = None,
        source: RetrievalSource,
        reference_store: ReferenceStore | None = None,
        candidate_limit: int = 8,
        per_query_limit: int | None = None,
        max_provider_requests: int = 6,
        fallback_protection: bool = True,
        legacy_candidate_stop: bool = False,
        full_text_limit: int = 8,
        max_concurrency: int = 4,
    ) -> None:
        if source.query_adapter is None:
            raise ValueError("RetrievalSource.query_adapter is required")
        if source.search_tool is None:
            raise ValueError("RetrievalSource.search_tool is required")
        if (candidate_limit < 1 or full_text_limit < 0 or max_concurrency < 1
                or (per_query_limit is not None and per_query_limit < 1)
                or max_provider_requests < 1):
            raise ValueError("retrieval limits and concurrency are invalid")
        # LEGACY / UNUSED: 旧版会在数据库检索阶段再次调用 Planner。
        # 当前正式链由 TaskResearchRequest.search_plan 注入唯一计划。
        # 仅为历史构造器兼容保留；新代码禁止依赖。
        self.search_planner = search_planner
        self.source = source
        self.reference_store = reference_store or ReferenceStore()
        self.candidate_limit = candidate_limit
        self.per_query_limit = per_query_limit or candidate_limit
        self.max_provider_requests = max_provider_requests
        self.fallback_protection = fallback_protection
        self.legacy_candidate_stop = legacy_candidate_stop
        self.full_text_limit = full_text_limit
        self.max_concurrency = max_concurrency
        self.source_id = source.source_id
        self.adapter = StructuredRetrievalAdapter()
        self._full_text_states = weakref.WeakKeyDictionary()

    async def acquire_full_texts(
        self, request: FullTextAcquisitionRequest, record_ids: list[str]
    ) -> ResearchBundle:
        """Acquire only previously discovered database records; never execute a search."""
        request = FullTextAcquisitionRequest.model_validate(request)
        if request.source_id != self.source_id:
            raise ValueError(
                f"request source_id {request.source_id!r} does not match "
                f"tool source_id {self.source_id!r}"
            )
        failures = []
        manifest = self.reference_store.load_manifest(request.subject_paper_id)
        records = {r.source_record_id: r for r in manifest.source_records}
        works = {w.work_id: w for w in manifest.works}
        selected = []
        for record_id in dict.fromkeys(record_ids):
            record = records.get(record_id)
            if (record is None or record.source_id != self.source_id
                    or record.source_kind != SourceKind.STRUCTURED_DATABASE
                    or not record.external_id or record.work_id not in works):
                raise ValueError(f"unknown or invalid database source record: {record_id}")
            selected.append(record)
        artifacts = []
        ready = set()
        for artifact in manifest.artifacts:
            if artifact.source_record_id not in record_ids:
                continue
            try:
                self.reference_store.verify_artifact_file(
                    request.subject_paper_id, artifact.artifact_id
                )
            except (OSError, ValueError) as exc:
                if artifact.role == ArtifactRole.EXTRACTED_TEXT:
                    from ...core.failure_classification import material_failure_code

                    failures.append(make_failure(
                        material_failure_code(exc),
                        scope=_failure_scope(
                            request, source_record_id=artifact.source_record_id
                        ),
                        message="Persisted full-text Artifact failed integrity verification; reacquisition was attempted.",
                        occurrence_id=f"fulltext-artifact-{artifact.artifact_id}",
                    ))
                continue
            artifacts.append(artifact)
            if artifact.role == ArtifactRole.EXTRACTED_TEXT:
                ready.add(artifact.source_record_id)
        hits = [(r.source_record_id, SearchHit(document_id=r.provenance.get("document_id") or r.external_id,
                 source_id=self.source_id, title=r.title)) for r in selected
                if r.source_record_id not in ready]
        from .providers.arxiv_scheduler import provider_task_id
        token = provider_task_id.set(request.research_task.task_id)
        try:
            full_texts, warnings = await self._fetch_full_texts(hits, request=request, failure_sink=failures)
        finally:
            provider_task_id.reset(token)
        for record in selected:
            full_text = full_texts.get(record.source_record_id)
            if full_text is None or not full_text.text.strip():
                continue
            try:
                extent = ContentExtent(full_text.content_extent)
            except ValueError:
                extent = ContentExtent.UNKNOWN
                warnings.append(f"full text {full_text.document_id}: unknown content extent")
            artifact = self._try_save_artifact(
                request.subject_paper_id, work=works[record.work_id], record=record,
                role=ArtifactRole.EXTRACTED_TEXT, content=full_text.text,
                extent=extent,
                version_label=full_text.version_label,
                provenance={"source": "full_text_tool", "source_url": full_text.source_url,
                            "media_type_received": full_text.media_type}, warnings=warnings)
            if artifact is not None:
                artifacts.append(artifact)
                ready.add(record.source_record_id)
            else:
                failures.append(make_failure(
                    FailureCode.FULLTEXT_FAILED,
                    scope=_failure_scope(
                        request, source_record_id=record.source_record_id
                    ),
                    message="Full-text content was returned but its verified Artifact could not be persisted.",
                    occurrence_id=f"fulltext-persist-{record.source_record_id}",
                ))
        selected = [r.model_copy(update={"access_status": AccessStatus.FULL_TEXT_ACQUIRED})
                    if r.source_record_id in ready else r for r in selected]
        if self.source.full_text_tool is None:
            warnings.append("full_text_tool unavailable; existing abstracts retained")
        self.reference_store.merge_manifest(request.subject_paper_id,
            source_records=selected, artifacts=artifacts)
        return ResearchBundle(
            bundle_id=self.adapter.stable_id("bnd", request.subject_paper_id,
                                            "full_text", *record_ids),
            producer=f"{self.name}:{self.source_id}",
            works=[works[r.work_id] for r in selected], source_records=selected,
            artifacts=artifacts, warnings=warnings, execution_failures=failures)

    async def ainvoke(
        self, request: StructuredSourceRetrievalRequest
    ) -> ResearchBundle:
        from .providers.arxiv_scheduler import provider_task_id

        request = StructuredSourceRetrievalRequest.model_validate(request)
        token = provider_task_id.set(request.research_task.task_id)
        try:
            return await self._ainvoke_with_task(request)
        finally:
            provider_task_id.reset(token)

    async def _ainvoke_with_task(
        self, request: StructuredSourceRetrievalRequest
    ) -> ResearchBundle:
        if request.source_id != self.source_id:
            raise ValueError(
                f"request source_id {request.source_id!r} does not match "
                f"tool source_id {self.source_id!r}"
            )
        plan = request.search_plan
        chain = build_fallback_chain(plan, preserve_protected=self.fallback_protection)
        pending, failed, unique_hits, chain_warnings = await self._search(chain, request)
        runtime = current_runtime_artifacts()
        if runtime is not None:
            runtime.record_retrieval_event({
                "phase": "candidate_selection", "source_id": self.source_id,
                "point_id": request.research_task.novelty_point_id,
                "task_id": request.research_task.task_id,
                "attempt": request.research_task.attempt,
                "candidate_limit": self.candidate_limit,
                "selected_keys": list(unique_hits),
                "observed_by_execution": [
                    {"execution_id": item.execution_id,
                     "observed_keys": [_candidate_key(hit) for hit in item.hits]}
                    for item in pending
                ],
            })
        warnings = list(chain_warnings)
        failures = []
        enriched_hits, acquisition_warnings = await self._enrich_metadata(unique_hits, request=request, failure_sink=failures)
        warnings.extend(acquisition_warnings)

        observed_at = datetime.now(timezone.utc)
        mapped: dict[str, tuple[Work, SourceRecord]] = {}
        works: dict[str, Work] = {}
        records: dict[str, SourceRecord] = {}
        for key, hit in enriched_hits.items():
            try:
                work, record, hit_warnings = self.adapter.adapt_hit(
                    hit, self.source_id, observed_at
                )
            except Exception as exc:
                warnings.append(
                    f"candidate {hit.document_id or hit.title!r} could not be mapped: {_safe_error(exc)}"
                )
                continue
            warnings.extend(hit_warnings)
            merge_work(works, work)
            merge_record(records, record)
            mapped[key] = (work, record)

        executions = [*failed]
        for item in pending:
            results: list[SearchResultRef] = []
            seen_records: set[str] = set()
            partial = False
            for rank, original_hit in enumerate(item.hits, start=1):
                pair = mapped.get(_candidate_key(original_hit))
                if pair is None:
                    partial = True
                    continue
                record_id = pair[1].source_record_id
                if record_id in seen_records:
                    partial = True
                    warnings.append(
                        f"search execution {item.execution_id} returned duplicate source record {record_id}"
                    )
                    continue
                seen_records.add(record_id)
                results.append(SearchResultRef(source_record_id=record_id, rank=rank))
            executions.append(
                SearchExecution(
                    execution_id=item.execution_id,
                    run_id=request.run_id,
                    tool_name=self.name,
                    source_id=self.source_id,
                    query=item.query.query,
                    parameters={
                        **_query_parameters(item.query, self.per_query_limit,
                                            variant=item.variant),
                        "requests_budget": self.max_provider_requests,
                        "candidate_output_limit": self.candidate_limit,
                        "observed_hits": [
                            {"rank": rank, "document_id": hit.document_id,
                             "candidate_key": _candidate_key(hit),
                             "selection": ("selected" if _candidate_key(hit) in mapped
                                           else "not_selected_or_unmapped")}
                            for rank, hit in enumerate(item.hits, start=1)
                        ],
                    },
                    status=(
                        SearchExecutionStatus.PARTIAL
                        if partial
                        else SearchExecutionStatus.SUCCEEDED
                    ),
                    started_at=item.started_at,
                    completed_at=item.completed_at,
                    results=results,
                )
            )

        executions.sort(key=lambda item: (item.started_at, item.execution_id))

        artifacts: dict[str, Artifact] = {}
        for key, hit in enriched_hits.items():
            pair = mapped.get(key)
            if pair is None or not hit.abstract.strip():
                continue
            work, record = pair
            artifact = self._try_save_artifact(
                request.subject_paper_id,
                work=work,
                record=record,
                role=ArtifactRole.ABSTRACT,
                content=hit.abstract,
                extent=ContentExtent.FULL,
                provenance={"source": "search_hit.abstract"},
                warnings=warnings,
            )
            if artifact is not None:
                artifacts[artifact.artifact_id] = artifact

        full_texts, full_text_warnings = await self._fetch_full_texts(
            list(enriched_hits.items())[: self.full_text_limit], request=request, failure_sink=failures
        )
        warnings.extend(full_text_warnings)
        if self.source.full_text_tool is None and any(
            hit.abstract.strip() for hit in enriched_hits.values()
        ):
            warnings.append(
                "full_text_tool is unavailable; saved abstracts only"
            )
        for key, full_text in full_texts.items():
            pair = mapped.get(key)
            if pair is None or not full_text.text.strip():
                continue
            work, record = pair
            try:
                extent = ContentExtent(full_text.content_extent)
            except ValueError:
                extent = ContentExtent.UNKNOWN
                warnings.append(
                    f"full text {full_text.document_id} has invalid content_extent; unknown was used"
                )
            artifact = self._try_save_artifact(
                request.subject_paper_id,
                work=work,
                record=record,
                role=ArtifactRole.EXTRACTED_TEXT,
                content=full_text.text,
                extent=extent,
                version_label=full_text.version_label,
                provenance={
                    "source": "full_text_tool",
                    "source_url": full_text.source_url,
                    "media_type_received": full_text.media_type,
                },
                warnings=warnings,
            )
            if artifact is not None:
                artifacts[artifact.artifact_id] = artifact
                records[record.source_record_id] = record.model_copy(
                    update={"access_status": AccessStatus.FULL_TEXT_ACQUIRED}
                )

        # 在锁内重新读取再合并：本方法中间有多次 await，其它并发任务可能已经写入，
        # 直接整份覆盖会丢掉它们的 Work/Artifact（reader 随后报 unknown artifact_id）。
        self.reference_store.merge_manifest(
            request.subject_paper_id,
            works=list(works.values()),
            source_records=list(records.values()),
            artifacts=list(artifacts.values()),
        )
        return ResearchBundle(
            bundle_id=self.adapter.stable_id(
                "bnd",
                request.subject_paper_id,
                request.run_id or "",
                request.research_task.novelty_point_id,
                request.research_task.task_id,
                self.source_id,
            ),
            producer=f"{self.name}:{self.source_id}",
            search_executions=sorted(
                executions, key=lambda value: value.started_at
            ),
            works=list(works.values()),
            source_records=list(records.values()),
            artifacts=list(artifacts.values()),
            evidence=[],
            warnings=list(dict.fromkeys(warnings)),
            execution_failures=failures,
        )

    def invoke(
        self, request: StructuredSourceRetrievalRequest
    ) -> ResearchBundle:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(self.ainvoke(request))
        raise RuntimeError(
            "检测到正在运行的事件循环，请改用 await tool.ainvoke(...)"
        )

    def _compile_variant(
        self,
        variant: FallbackVariant,
        plan: SearchPlan,
    ) -> list[CompiledQuery]:
        """把一条放宽变体编译为数据库查询（纯代码，不执行）。"""

        single = plan.model_copy(
            update={
                "strategies": [
                    SearchStrategy(
                        strategy_id=variant.variant_id,
                        level=variant.level,
                        expression=variant.expression,
                        description="",
                        use_alias=variant.use_alias,
                        use_exclude=variant.use_exclude,
                    )
                ]
            }
        )
        try:
            return list(self.source.query_adapter.compile(single))
        except Exception as exc:
            return []

    async def _search(
        self,
        chain: Sequence[FallbackVariant],
        request: StructuredSourceRetrievalRequest,
    ) -> tuple[
        list[_PendingExecution],
        list[SearchExecution],
        dict[str, SearchHit],
        list[str],
    ]:
        """Run base directions first, then eligible fallbacks under a request budget."""

        pending: list[_PendingExecution] = []
        failed: list[SearchExecution] = []
        warnings: list[str] = []
        base_hit: dict[str, bool] = {}
        execution_index = 0
        provider_failed = False
        blocking_execution: SearchExecution | None = None
        physical_budget_exhausted = False

        ordered = list(chain) if self.legacy_candidate_stop else [
            v for v in chain if v.variant_id == v.base_strategy_id
        ] + [v for v in chain if v.variant_id != v.base_strategy_id]
        fallback_bases = {v.base_strategy_id for v in chain
                          if v.variant_id != v.base_strategy_id}
        for variant in ordered:
            if (variant.variant_id == variant.base_strategy_id
                    and variant.level != "broad"
                    and variant.base_strategy_id not in fallback_bases):
                warnings.append(
                    f"{variant.base_strategy_id}: fallback_not_applicable; "
                    "no safe removable concept"
                )
        requests_used = 0
        legacy_unique: dict[str, SearchHit] = {}
        runtime = current_runtime_artifacts()
        from ...core.local_correction import retry_queries
        frozen_queries = retry_queries.get()
        retried_queries = set()
        for variant in ordered:
            if self.legacy_candidate_stop and len(legacy_unique) >= self.candidate_limit:
                break
            if (
                variant.variant_id != variant.base_strategy_id
                and base_hit.get(variant.base_strategy_id)
            ):
                warnings.append(f"{variant.variant_id}: fallback_not_applicable; base strategy hit")
                continue
            compiled = self._compile_variant(variant, request.search_plan)
            if not compiled:
                warnings.append(
                    f"fallback variant {variant.variant_id} failed to compile; skipped"
                )
                continue
            for query in compiled:
                if frozen_queries is not None:
                    if query.query not in frozen_queries or query.query in retried_queries:
                        continue
                    retried_queries.add(query.query)
                execution_index += 1
                started = datetime.now(timezone.utc)
                execution_id = self.adapter.stable_id(
                    "sex",
                    request.run_id or request.subject_paper_id,
                    request.research_task.task_id,
                    query.strategy_id,
                    query.query,
                    str(execution_index),
                )
                if provider_failed or physical_budget_exhausted or requests_used >= self.max_provider_requests:
                    reason = ("retrieval_incomplete_budget" if physical_budget_exhausted
                              or requests_used >= self.max_provider_requests
                              else "provider_failed")
                    failed.append(SearchExecution(
                        execution_id=execution_id, run_id=request.run_id,
                        tool_name=self.name, source_id=self.source_id,
                        query=query.query,
                        parameters={**_query_parameters(query, self.per_query_limit, variant=variant),
                                    "not_run_reason": reason,
                                    **({"blocked_by_execution_id": blocking_execution.execution_id,
                                        "blocked_by_failure_event_id": blocking_execution.failure.event_id,
                                        "blocked_by_failure_code": blocking_execution.failure.code.value}
                                       if reason == "provider_failed" and blocking_execution is not None
                                       and blocking_execution.failure is not None else {}),
                                    "requests_used": requests_used,
                                    "max_provider_requests": self.max_provider_requests},
                        status=SearchExecutionStatus.NOT_RUN,
                        failure=make_failure(FailureCode.RETRIEVAL_BUDGET if reason == "retrieval_incomplete_budget" else FailureCode.RETRIEVAL_SKIPPED,
                            scope=_failure_scope(request), message="Query was not executed after an observed source or budget boundary.", occurrence_id=execution_id),
                        started_at=started, completed_at=started,
                    ))
                    if runtime is not None:
                        runtime.record_retrieval_event({
                            "phase": "provider_query", "execution_id": execution_id,
                            "status": "NOT_RUN", "reason": reason,
                            "query": query.query, "limit": self.per_query_limit,
                        })
                    continue
                requests_used += 1
                if runtime is not None:
                    runtime.record_retrieval_event({
                        "phase": "provider_query", "execution_id": execution_id,
                        "status": "START", "query": query.query,
                        "limit": self.per_query_limit,
                        "strategy_id": query.strategy_id,
                        "requests_used": requests_used,
                        "max_provider_requests": self.max_provider_requests,
                    })
                try:
                    raw_hits = list(
                        await _invoke_provider(
                            self.source.search_tool.search,
                            query.query,
                            limit=self.per_query_limit,
                        )
                    )
                    hits = [
                        hit if isinstance(hit, SearchHit) else SearchHit(**hit)
                        for hit in raw_hits
                    ]
                except ProviderPhysicalBudgetExceeded:
                    requests_used -= 1
                    physical_budget_exhausted = True
                    failed.append(SearchExecution(
                        execution_id=execution_id, run_id=request.run_id,
                        tool_name=self.name, source_id=self.source_id,
                        query=query.query,
                        parameters={**_query_parameters(query, self.per_query_limit,
                                                        variant=variant),
                                    "not_run_reason": "retrieval_incomplete_budget",
                                    "budget_unit": "physical_provider_request"},
                        status=SearchExecutionStatus.NOT_RUN,
                        failure=make_failure(FailureCode.RETRIEVAL_BUDGET, scope=_failure_scope(request),
                            message="Physical provider request budget refused this query before execution.", occurrence_id=execution_id),
                        started_at=started,
                        completed_at=datetime.now(timezone.utc),
                    ))
                    if runtime is not None:
                        runtime.record_retrieval_event({
                            "phase": "provider_query", "execution_id": execution_id,
                            "status": "NOT_RUN", "reason": "retrieval_incomplete_budget",
                            "budget_unit": "physical_provider_request",
                        })
                    continue
                except ReplayMissError:
                    requests_used -= 1
                    failed.append(SearchExecution(
                        execution_id=execution_id, run_id=request.run_id,
                        tool_name=self.name, source_id=self.source_id,
                        query=query.query,
                        parameters={**_query_parameters(query, self.per_query_limit,
                                                        variant=variant),
                                    "not_run_reason": "replay_miss"},
                        status=SearchExecutionStatus.NOT_RUN,
                        failure=make_failure(FailureCode.RETRIEVAL_SKIPPED, scope=_failure_scope(request),
                            message="Offline replay has no exact captured response; query did not execute.", occurrence_id=execution_id),
                        started_at=started,
                        completed_at=datetime.now(timezone.utc),
                    ))
                    if runtime is not None:
                        runtime.record_retrieval_event({
                            "phase": "provider_query", "execution_id": execution_id,
                            "status": "REPLAY_MISS", "capture_boundary": "offline_fixture",
                        })
                    continue
                except Exception as exc:
                    if runtime is not None:
                        runtime.record_retrieval_event({
                            "phase": "provider_query", "execution_id": execution_id,
                            "status": "FAILED", "error": _safe_error(exc),
                        })
                    failed.append(
                        SearchExecution(
                            execution_id=execution_id,
                            run_id=request.run_id,
                            tool_name=self.name,
                            source_id=self.source_id,
                            query=query.query,
                            parameters=_query_parameters(
                                query, self.per_query_limit, variant=variant
                            ),
                            status=SearchExecutionStatus.FAILED,
                            started_at=started,
                            completed_at=datetime.now(timezone.utc),
                            error=_safe_error(exc),
                            failure=provider_failure(exc, scope=FailureScope(
                                paper_id=request.subject_paper_id, run_id=request.run_id,
                                point_id=request.novelty_point.point_id,
                                task_id=request.research_task.task_id,
                                attempt=request.research_task.attempt, provider=self.source_id,
                            ), occurrence_id=execution_id),
                        )
                    )
                    blocking_execution = failed[-1]
                    provider_failed = True
                    continue
                pending.append(
                    _PendingExecution(
                        execution_id=execution_id,
                        query=query,
                        started_at=started,
                        completed_at=datetime.now(timezone.utc),
                        hits=hits,
                        variant=variant,
                    )
                )
                if runtime is not None:
                    runtime.record_retrieval_event({
                        "phase": "provider_query", "execution_id": execution_id,
                        "status": "SUCCEEDED", "capture_boundary": "SearchTool.search",
                        "raw_result": raw_hits, "normalized_hits": hits,
                    })
                if hits:
                    base_hit[variant.base_strategy_id] = True
                if self.legacy_candidate_stop:
                    for hit in hits:
                        legacy_unique.setdefault(_candidate_key(hit), hit)
                    if len(legacy_unique) >= self.candidate_limit:
                        break
        # Rotate among base directions, then their fallbacks. A fast or early
        # response cannot monopolize the bounded final candidate set.
        groups: dict[str, list[SearchHit]] = {}
        for item in pending:
            group = groups.setdefault(item.variant.base_strategy_id, [])
            group.extend(item.hits)
        unique: dict[str, SearchHit] = {}
        while any(groups.values()) and len(unique) < self.candidate_limit:
            for group in groups.values():
                if group:
                    hit = group.pop(0)
                    unique.setdefault(_candidate_key(hit), hit)
                    if len(unique) >= self.candidate_limit:
                        break
        if requests_used >= self.max_provider_requests and any(
            item.status == SearchExecutionStatus.NOT_RUN for item in failed
        ):
            warnings.append("retrieval_incomplete_budget")
        if self.legacy_candidate_stop:
            unique = dict(list(legacy_unique.items())[:self.candidate_limit])

        return (
            pending,
            failed,
            unique,
            warnings,
        )

    async def _enrich_metadata(
        self, hits: Mapping[str, SearchHit], *, request=None, failure_sink=None
    ) -> tuple[dict[str, SearchHit], list[str]]:
        if self.source.metadata_tool is None:
            return dict(hits), []
        semaphore = asyncio.Semaphore(self.max_concurrency)

        async def enrich(key: str, hit: SearchHit) -> tuple[str, SearchHit, str | None]:
            async with semaphore:
                try:
                    metadata = await _invoke_provider(
                        self.source.metadata_tool.resolve,
                        hit.document_id,
                    )
                except Exception as exc:
                    if request is not None and failure_sink is not None:
                        failure_sink.append(provider_failure(exc, scope=_failure_scope(request),
                            occurrence_id=f"metadata-{hit.document_id}").model_copy(update={"execution_status":"partial"}))
                    return key, hit, f"metadata {hit.document_id}: {_safe_error(exc)}"
            if metadata is None:
                return key, hit, None
            return key, replace(
                hit,
                title=metadata.title or hit.title,
                doi=metadata.doi or hit.doi,
                url=metadata.url or hit.url,
            ), None

        results = await asyncio.gather(
            *(enrich(key, hit) for key, hit in hits.items())
        )
        return (
            {key: hit for key, hit, _ in results},
            [warning for _, _, warning in results if warning is not None],
        )

    async def _fetch_full_texts(
        self, hits: Sequence[tuple[str, SearchHit]], *, request=None, failure_sink=None
    ) -> tuple[dict[str, FullText], list[str]]:
        if self.source.full_text_tool is None:
            if request is not None and failure_sink is not None and hits:
                for record_id, hit in hits:
                    failure_sink.append(make_failure(
                        FailureCode.FULLTEXT_UNSUPPORTED,
                        scope=_failure_scope(request, source_record_id=record_id),
                        message="This configured source has no full-text acquisition capability; available abstracts remain usable.",
                        occurrence_id=f"fulltext-capability-{hit.document_id}",
                    ))
            return {}, []
        loop = asyncio.get_running_loop()
        if loop not in self._full_text_states:
            self._full_text_states[loop] = (asyncio.Semaphore(self.max_concurrency), {})
        semaphore, pending = self._full_text_states[loop]

        async def acquire(document_id):
            async with semaphore:
                return await _invoke_provider(self.source.full_text_tool.fetch, document_id)

        async def fetch(key: str, hit: SearchHit) -> tuple[str, FullText | None, str | None]:
            document_id = hit.document_id
            if document_id not in pending:
                task = asyncio.create_task(acquire(document_id))
                pending[document_id] = task
                def done(completed, document_id=document_id):
                    pending.pop(document_id, None)
                    if not completed.cancelled():
                        completed.exception()
                task.add_done_callback(done)
            try:
                value = await asyncio.shield(pending[document_id])
                if (value is None or not value.text.strip()) and request is not None and failure_sink is not None:
                    failure_sink.append(make_failure(FailureCode.FULLTEXT_EMPTY, scope=_failure_scope(request, source_record_id=key),
                        message="Full-text operation returned no readable material; this is not a search no-match finding.",
                        occurrence_id=f"fulltext-{document_id}"))
                return key, value, None
            except Exception as exc:
                if request is not None and failure_sink is not None:
                    cause = provider_failure(exc, scope=_failure_scope(request, source_record_id=key), occurrence_id=f"fulltext-{document_id}")
                    failure_sink.extend([cause, make_failure(FailureCode.FULLTEXT_FAILED, scope=_failure_scope(request, source_record_id=key),
                        message="Full-text acquisition failed; available abstract material remains usable.",
                        cause_event_ids=[cause.event_id], occurrence_id=f"fulltext-{document_id}")])
                return key, None, f"full text {document_id}: {_safe_error(exc)}"

        results = await asyncio.gather(*(fetch(key, hit) for key, hit in hits))
        return (
            {key: value for key, value, _ in results if value is not None},
            [
                warning
                for _, _, warning in results
                if warning is not None
            ]
            + [
                f"full text {hit.document_id}: no content returned; abstract only"
                for (key, hit), (result_key, value, warning) in zip(hits, results)
                if key == result_key and value is None and warning is None
            ],
        )

    def _try_save_artifact(
        self,
        paper_id: str,
        *,
        work: Work,
        record: SourceRecord,
        role: ArtifactRole,
        content: str,
        extent: ContentExtent,
        provenance: dict[str, Any],
        warnings: list[str],
        version_label: str | None = None,
    ) -> Artifact | None:
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        artifact_id = self.adapter.stable_id(
            "art", work.work_id, role.value, digest
        )
        try:
            self.reference_store.write_document(
                paper_id,
                work_id=work.work_id,
                artifact_id=artifact_id,
                extension="txt",
                content=content,
            )
        except Exception as exc:
            try:
                self.reference_store.repair_corrupt_document(
                    paper_id,
                    work_id=work.work_id,
                    artifact_id=artifact_id,
                    content=content,
                )
                warnings.append(
                    f"artifact {artifact_id} was restored from exact provider content after integrity failure"
                )
            except Exception:
                warnings.append(
                    f"artifact {artifact_id} could not be saved: {_safe_error(exc)}"
                )
                return None
        return Artifact(
            artifact_id=artifact_id,
            work_id=work.work_id,
            source_record_id=record.source_record_id,
            role=role,
            media_type="text/plain",
            relative_path=f"documents/{work.work_id}/{artifact_id}.txt",
            sha256=digest,
            byte_size=len(content.encode("utf-8")),
            version_label=version_label,
            content_extent=extent,
            acquired_at=datetime.now(timezone.utc),
            provenance=provenance,
        )


def _candidate_key(hit: SearchHit) -> str:
    external_id = (hit.external_id or hit.document_id).strip()
    if external_id:
        return f"source:{hit.source_id or ''}:{external_id}"
    if hit.doi:
        return f"doi:{_normalize_doi(hit.doi)}"
    if hit.url:
        return f"url:{hit.url.casefold().rstrip('/')}"
    return f"title:{_normalize_text(hit.title)}"


def _query_parameters(
    query: CompiledQuery,
    limit: int,
    *,
    variant: FallbackVariant | None = None,
) -> dict[str, Any]:
    parameters = {
        "limit": limit,
        "task_id": query.task_id,
        "novelty_point_id": query.novelty_point_id,
        "strategy_id": query.strategy_id,
        "level": query.level,
    }
    if variant is not None:
        parameters["variant_id"] = variant.variant_id
        parameters["base_strategy"] = variant.base_strategy_id
        parameters["fallback_reason"] = variant.drop_reason or None
    return parameters


def _normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip().casefold()


def _normalize_doi(value: str) -> str:
    normalized = value.strip().casefold()
    for prefix in ("https://doi.org/", "http://doi.org/", "doi:"):
        if normalized.startswith(prefix):
            return normalized[len(prefix) :].strip()
    return normalized


def _safe_error(exc: Exception) -> str:
    message = re.sub(
        r"(?i)(api[_-]?key|authorization|cookie)\s*[:=]\s*\S+",
        r"\1=<redacted>",
        str(exc),
    )
    return f"{type(exc).__name__}: {message}"[:1000]


def _failure_scope(request, *, source_record_id=None):
    return FailureScope(paper_id=request.subject_paper_id, run_id=request.run_id,
        point_id=request.novelty_point.point_id, task_id=request.research_task.task_id,
        attempt=request.research_task.attempt, provider=request.source_id,
        source_record_id=source_record_id)
