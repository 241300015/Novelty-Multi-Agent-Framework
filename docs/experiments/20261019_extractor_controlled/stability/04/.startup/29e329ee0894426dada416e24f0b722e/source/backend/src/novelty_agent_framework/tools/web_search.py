"""Agent-facing Web discovery tool over a provider-neutral search backend."""

from __future__ import annotations

import hashlib
import time
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit, urlunsplit
from pydantic import Field, create_model

from ..persistence import ReferenceStore
from ..schemas import (
    AccessStatus,
    ResearcherToolObservation,
    SourceKind,
    SourceRecord,
    TaskResearchRequest,
    WebSearchArguments,
    WebSearchItem,
    WebSearchResult,
)
from ..schemas.research_tools import NonEmptyStr
from .web_search_backend import SearchBackend, SearchHit, _validate_baidu_query, BaiduSearchError


class WebSearchTool:
    name = "web_search"
    description = "搜索 Web 补充资料；搜索结果本身不是证据。query只填写短关键词，不要解释、推理或整段研究任务。"
    args_schema = WebSearchArguments

    def __init__(
        self,
        backend: SearchBackend,
        reference_store: ReferenceStore | None = None,
        *,
        default_max_results: int = 10,
        max_results_per_call: int = 50,
    ) -> None:
        if not backend.name.strip():
            raise ValueError("search backend name cannot be empty")
        self.backend = backend
        query_description = "只传检索词，不传解释、推理过程或任务全文。"
        if backend.name == "baidu":
            query_description += ("最多72单位：ASCII字符含空格计1，非ASCII计2。建议不超过60单位。"
                                  "示例：图摘要 分布式GNN；graph summarization distributed GNN。")
            self.description += query_description
        self.reference_store = reference_store or ReferenceStore()
        if not 1 <= default_max_results <= max_results_per_call <= 100:
            raise ValueError("web search result limits are invalid")
        self.default_max_results = default_max_results
        self.max_results_per_call = max_results_per_call
        self.args_schema = create_model(
            f"ConfiguredWebSearchArguments{default_max_results}_{max_results_per_call}",
            __base__=WebSearchArguments,
            query=(NonEmptyStr, Field(description=query_description)),
            max_results=(int, Field(default=default_max_results, ge=1, le=max_results_per_call)),
        )

    async def ainvoke(
        self,
        arguments: WebSearchArguments,
        *,
        scope: TaskResearchRequest,
    ) -> ResearcherToolObservation:
        started = time.monotonic()
        max_results = arguments.max_results
        if max_results > self.max_results_per_call:
            raise ValueError(
                f"max_results exceeds web_search limit {self.max_results_per_call}"
            )
        if self.backend.name == "baidu":
            try:
                _validate_baidu_query(arguments.query)
            except BaiduSearchError as exc:
                return ResearcherToolObservation(
                    tool_name=self.name, arguments=arguments.model_dump(mode="json"), succeeded=False,
                    error=str(exc), summary="查询未发送，请缩短关键词后重试，不要原样重复。",
                    payload={"error_code": "INVALID_QUERY", "query_units": sum(
                        1 if ord(c) < 128 else 2 for c in arguments.query.strip()),
                        "max_query_units": 72, "retry_instruction":
                        "ASCII（含空格）计1，非ASCII计2；只传短关键词，例如 graph summarization distributed GNN。不要加入解释或推理。"},
                )
        backend_result = await self.backend.search(
            arguments.query,
            max_results=max_results,
        )
        observed_at = datetime.now(timezone.utc)
        manifest = self.reference_store.load_manifest(scope.subject_paper_id)
        records_by_id = {
            record.source_record_id: record for record in manifest.source_records
        }
        items: list[WebSearchItem] = []

        for rank, hit in enumerate(backend_result.hits, start=1):
            source_record_id = _source_record_id(self.backend.name, hit.url)
            current = records_by_id.get(source_record_id)
            record = _source_record(
                source_record_id=source_record_id,
                backend_name=self.backend.name,
                hit=hit,
                observed_at=observed_at,
                run_id=scope.run_id,
                query=arguments.query,
                rank=rank,
                current=current,
            )
            records_by_id[source_record_id] = record
            items.append(
                WebSearchItem(
                    source_record_id=source_record_id,
                    rank=rank,
                    title=hit.title,
                    url=hit.url,
                    snippet=hit.snippet,
                    score=hit.score,
                    published_at=hit.published_at,
                    source_name=hit.source_name,
                )
            )

        updated_manifest = manifest.model_copy(
            update={
                "source_records": list(records_by_id.values()),
                "updated_at": observed_at,
            }
        )
        self.reference_store.persist_manifest(
            scope.subject_paper_id,
            updated_manifest,
        )
        result = WebSearchResult(
            query=backend_result.query,
            results=items,
            warnings=list(backend_result.warnings),
        )
        return ResearcherToolObservation(
            tool_name=self.name,
            arguments=arguments.model_dump(mode="json"),
            succeeded=True,
            summary=f"发现并保存 {len(items)} 个 Web 候选来源",
            payload={
                "search_result": result.model_dump(mode="json"),
                "source_records": [
                    records_by_id[item.source_record_id].model_dump(mode="json")
                    for item in items
                ],
            },
            elapsed_ms=int((time.monotonic() - started) * 1_000),
        )

    def project_model_context(
        self, observation: ResearcherToolObservation
    ) -> dict[str, Any]:
        if not observation.succeeded:
            return {"succeeded": False, "error": observation.error, **observation.payload}
        result = observation.payload["search_result"]
        return {
            "succeeded": observation.succeeded,
            "summary": observation.summary,
            "query": result["query"],
            "results": [
                {
                    key: item.get(key)
                    for key in (
                        "source_record_id",
                        "title",
                        "url",
                        "snippet",
                        "source_name",
                        "published_at",
                    )
                }
                for item in result["results"]
            ],
            "warnings": result["warnings"],
        }


def _source_record(
    *,
    source_record_id: str,
    backend_name: str,
    hit: SearchHit,
    observed_at: datetime,
    run_id: str,
    query: str,
    rank: int,
    current: SourceRecord | None,
) -> SourceRecord:
    raw_metadata: dict[str, Any] = (
        dict(current.raw_metadata) if current is not None else {}
    )
    raw_metadata.update(hit.raw_metadata)
    raw_metadata.update(
        {
            "search_snippet": hit.snippet,
            "search_score": hit.score,
            "published_at": (
                hit.published_at.isoformat() if hit.published_at is not None else None
            ),
            "source_name": hit.source_name,
        }
    )
    provenance = dict(current.provenance) if current is not None else {}
    provenance.update(
        {
            "tool": "web_search",
            "run_id": run_id,
            "query": query,
            "rank": rank,
        }
    )
    values = {
        "source_record_id": source_record_id,
        "work_id": current.work_id if current is not None else None,
        "source_id": backend_name,
        "source_kind": SourceKind.WEB_SUPPLEMENT,
        "external_id": (
            hit.external_id
            if hit.external_id is not None
            else (current.external_id if current is not None else None)
        ),
        "title": hit.title,
        "landing_url": hit.url,
        "access_status": (
            current.access_status
            if current is not None
            else AccessStatus.DISCOVERED
        ),
        "raw_metadata": raw_metadata,
        "observed_at": observed_at,
        "provenance": provenance,
    }
    if current is None:
        return SourceRecord(**values)
    return current.model_copy(update=values)


def _source_record_id(backend_name: str, url: str) -> str:
    normalized = _normalized_url(url)
    digest = hashlib.sha256(f"{backend_name}\0{normalized}".encode()).hexdigest()
    return f"src_{digest[:24]}"


def _normalized_url(url: str) -> str:
    value = url.strip()
    parts = urlsplit(value)
    scheme = parts.scheme.lower()
    hostname = (parts.hostname or "").lower()
    if not scheme or not hostname:
        return value
    port = parts.port
    default_port = (scheme == "http" and port == 80) or (
        scheme == "https" and port == 443
    )
    host = hostname if port is None or default_port else f"{hostname}:{port}"
    path = parts.path or "/"
    return urlunsplit((scheme, host, path, parts.query, ""))
