"""Provider-neutral backend contract for Web resource discovery."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from math import isfinite
import os
from typing import Any, Mapping, Protocol, Sequence

import httpx

BAIDU_WEB_SEARCH_ENDPOINT = "https://qianfan.baidubce.com/v2/ai_search/web_search"


@dataclass(frozen=True)
class SearchHit:
    title: str
    url: str
    snippet: str | None = None
    score: float | None = None
    published_at: datetime | None = None
    source_name: str | None = None
    external_id: str | None = None
    raw_metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.title.strip():
            raise ValueError("search hit title cannot be empty")
        if not self.url.strip():
            raise ValueError("search hit url cannot be empty")
        _validate_json_value(self.raw_metadata, "raw_metadata")


@dataclass(frozen=True)
class SearchBackendResult:
    query: str
    hits: Sequence[SearchHit] = ()
    warnings: Sequence[str] = ()

    def __post_init__(self) -> None:
        if not self.query.strip():
            raise ValueError("search result query cannot be empty")


class SearchBackend(Protocol):
    name: str

    async def search(
        self,
        query: str,
        *,
        max_results: int,
    ) -> SearchBackendResult: ...


class BaiduSearchError(RuntimeError):
    """Baidu Web Search request or response failure without credential data."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        request_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.request_id = request_id


class BaiduSearchBackend:
    """Baidu Qianfan implementation of the frozen SearchBackend contract."""

    name = "baidu"

    def __init__(
        self,
        api_key: str | None = None,
        *,
        timeout_seconds: float = 30.0,
        endpoint: str = BAIDU_WEB_SEARCH_ENDPOINT,
    ) -> None:
        self.api_key = api_key or os.getenv("BAIDU_QIANFAN_API_KEY")
        self.timeout_seconds = timeout_seconds
        self.endpoint = endpoint
        self.last_diagnostics: dict[str, Any] = {}

    async def search(
        self,
        query: str,
        *,
        max_results: int,
    ) -> SearchBackendResult:
        normalized_query = _validate_baidu_query(query)
        if not 1 <= max_results <= 50:
            raise BaiduSearchError("max_results must be in 1..50 for Baidu search")
        if not self.api_key:
            raise BaiduSearchError("BAIDU_QIANFAN_API_KEY is not configured")

        payload = {
            "messages": [{"role": "user", "content": normalized_query}],
            "search_source": "baidu_search_v2",
            "resource_type_filter": [{"type": "web", "top_k": max_results}],
        }
        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
                response = await client.post(
                    self.endpoint,
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                )
        except httpx.TimeoutException as exc:
            raise BaiduSearchError("Baidu Web Search request timed out") from exc
        except httpx.RequestError as exc:
            raise BaiduSearchError("Baidu Web Search network request failed") from exc

        body = _response_json(response)
        request_id = _optional_string(body.get("request_id"))
        if response.status_code >= 400:
            raise BaiduSearchError(
                f"Baidu Web Search HTTP {response.status_code}",
                status_code=response.status_code,
                request_id=request_id,
            )
        code = body.get("code")
        if code not in (None, 0, "0"):
            message = _optional_string(body.get("message")) or "unknown business error"
            raise BaiduSearchError(
                f"Baidu Web Search business error {code}: {message}",
                request_id=request_id,
            )

        references = body.get("references", [])
        if references is None:
            references = []
        if not isinstance(references, list):
            raise BaiduSearchError(
                "Baidu Web Search references must be an array",
                request_id=request_id,
            )
        hits = [
            mapped
            for item in references
            if isinstance(item, Mapping)
            for mapped in [_map_baidu_reference(item, request_id=request_id)]
            if mapped is not None
        ]
        web_references = [
            item
            for item in references
            if isinstance(item, Mapping) and item.get("type") == "web"
        ]
        urls = [
            url
            for item in web_references
            if (url := _optional_string(item.get("url"))) is not None
        ]
        self.last_diagnostics = {
            "request_id": request_id,
            "raw_reference_count": len(references),
            "normalized_hit_count": len(hits),
            "skipped_non_web_count": len(references) - len(web_references),
            "skipped_missing_url_count": len(web_references) - len(urls),
            "missing_snippet_count": sum(
                _optional_string(item.get("snippet")) is None
                for item in web_references
                if _optional_string(item.get("url")) is not None
            ),
            "missing_date_count": sum(
                _optional_string(item.get("date")) is None
                for item in web_references
                if _optional_string(item.get("url")) is not None
            ),
            "missing_website_count": sum(
                _optional_string(item.get("website")) is None
                for item in web_references
                if _optional_string(item.get("url")) is not None
            ),
            "duplicate_url_count": len(urls) - len(set(urls)),
        }
        return SearchBackendResult(query=normalized_query, hits=hits, warnings=[])


def _validate_baidu_query(query: str) -> str:
    normalized = query.strip()
    if not normalized:
        raise BaiduSearchError("query cannot be empty")
    # Baidu documents Chinese characters as two units. For v1, all non-ASCII
    # code points use two units; ASCII code points use one.
    units = sum(1 if ord(character) < 128 else 2 for character in normalized)
    if units > 72:
        raise BaiduSearchError(f"query exceeds Baidu's 72-unit limit ({units})")
    return normalized


def _response_json(response: httpx.Response) -> Mapping[str, Any]:
    try:
        body = response.json()
    except (ValueError, TypeError) as exc:
        raise BaiduSearchError(
            "Baidu Web Search returned invalid JSON",
            status_code=response.status_code,
        ) from exc
    if not isinstance(body, Mapping):
        raise BaiduSearchError(
            "Baidu Web Search response must be a JSON object",
            status_code=response.status_code,
        )
    return body


def _map_baidu_reference(
    item: Mapping[str, Any],
    *,
    request_id: str | None,
) -> SearchHit | None:
    if item.get("type") != "web":
        return None
    url = _optional_string(item.get("url"))
    if url is None:
        return None
    title = _optional_string(item.get("title")) or url
    raw_metadata = {
        key: value
        for key, value in {
            "request_id": request_id,
            "web_anchor": item.get("web_anchor"),
            "content": item.get("content"),
        }.items()
        if value is not None
    }
    return SearchHit(
        title=title,
        url=url,
        snippet=_optional_string(item.get("snippet")),
        score=None,
        published_at=_parse_baidu_date(item.get("date")),
        source_name=_optional_string(item.get("website")),
        external_id=(
            str(item["id"]) if item.get("id") is not None else None
        ),
        raw_metadata=raw_metadata,
    )


def _parse_baidu_date(value: Any) -> datetime | None:
    text = _optional_string(value)
    if text is None:
        return None
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _optional_string(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized or None


def _validate_json_value(value: Any, path: str) -> None:
    if isinstance(value, float) and not isfinite(value):
        raise ValueError(f"{path} contains non-finite float")
    if value is None or isinstance(value, (str, int, float, bool)):
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _validate_json_value(item, f"{path}[{index}]")
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError(f"{path} keys must be strings")
            _validate_json_value(item, f"{path}.{key}")
        return
    raise ValueError(f"{path} contains non-JSON-compatible {type(value).__name__}")
