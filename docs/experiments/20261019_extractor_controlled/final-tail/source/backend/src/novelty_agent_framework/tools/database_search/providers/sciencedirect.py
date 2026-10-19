"""Elsevier ScienceDirect Search V2 and Article Retrieval provider.

Search V2 returns discovery metadata but no complete abstract.  This provider
keeps that API mismatch local: it enriches only the retained top candidates
through Article Retrieval ``META_ABS`` and returns ordinary ``SearchHit``
objects to the source-independent retrieval pipeline.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace
from typing import Any
from urllib.parse import quote

import httpx

from ....ports import FullText, FullTextTool, SearchHit, SearchTool
from ....schemas import EvidenceSource, SearchConcept, SearchPlan
from ..adapter import CompiledQuery, QueryAdapter, QueryAdapterError
from ..retrieval_sources import RetrievalSource
from .common import (
    HttpRequestPolicy,
    ProviderConfigurationError,
    ProviderRequestError,
    ResilientHttpClient,
    resolve_env_credential,
)

DEFAULT_BASE_URL = "https://api.elsevier.com"
SEARCH_PATH = "/content/search/sciencedirect"
ARTICLE_PII_PATH = "/content/article/pii/"
_VALID_PAGE_SIZES = (10, 25, 50, 100)


class ScienceDirectQueryAdapter(QueryAdapter):
    """Compile source-independent concepts into a Search V2 ``qs`` expression."""

    database = "sciencedirect"

    def __init__(self, *, max_query_chars: int = 250) -> None:
        if max_query_chars < 1:
            raise ValueError("max_query_chars must be positive")
        self.max_query_chars = max_query_chars

    def compile(self, plan: SearchPlan) -> Sequence[CompiledQuery]:
        compiled = list(super().compile(plan))
        for item in compiled:
            if len(item.query) > self.max_query_chars:
                raise QueryAdapterError(
                    "ScienceDirect qs query exceeds "
                    f"{self.max_query_chars} characters: {len(item.query)}"
                )
        return compiled

    def _render_concept(
        self,
        concept: SearchConcept,
        *,
        use_alias: bool = False,
        use_exclude: bool = True,
    ) -> str:
        terms = _normalized_unique(concept.terms)
        if use_alias:
            terms.extend(
                term for term in _normalized_unique(concept.alias) if term not in terms
            )
        if not terms:
            raise QueryAdapterError(f"Concept {concept.concept_id} has no usable terms")
        rendered = [_quote_query_term(term) for term in terms]
        block = rendered[0] if len(rendered) == 1 else f"({' OR '.join(rendered)})"
        excluded = _normalized_unique(concept.exclude) if use_exclude else []
        if excluded:
            block = (
                f"{block} AND NOT "
                f"({' OR '.join(_quote_query_term(term) for term in excluded)})"
            )
        return block


class ScienceDirectArticleClient:
    """PII-based metadata, abstract, and full-text acquisition with shared cache."""

    def __init__(
        self,
        transport: ResilientHttpClient,
        *,
        base_url: str,
        headers: Mapping[str, str],
    ) -> None:
        self.transport = transport
        self.base_url = base_url.rstrip("/")
        self.headers = dict(headers)
        self._metadata_cache: dict[str, dict[str, Any] | None] = {}

    def fetch_metadata(self, pii: str) -> dict[str, Any] | None:
        normalized = pii.strip()
        if normalized in self._metadata_cache:
            return self._metadata_cache[normalized]
        response = self.transport.get(
            self._article_url(normalized),
            params={"view": "META_ABS"},
            headers={**self.headers, "Accept": "application/json"},
        )
        if response.status_code in {403, 404}:
            self._metadata_cache[normalized] = None
            return None
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, Mapping):
            raise ValueError("ScienceDirect article response must be a JSON object")
        coredata = payload.get("full-text-retrieval-response", {}).get(
            "coredata", {}
        )
        result = dict(coredata) if isinstance(coredata, Mapping) else {}
        self._metadata_cache[normalized] = result or None
        return self._metadata_cache[normalized]

    def cached_metadata(self, pii: str) -> dict[str, Any] | None:
        """Return prior META_ABS data without issuing an additional request."""

        return self._metadata_cache.get(pii.strip())

    def fetch_full_text(self, pii: str) -> httpx.Response | None:
        response = self.transport.get(
            self._article_url(pii),
            params={"view": "FULL"},
            headers={**self.headers, "Accept": "text/plain"},
        )
        if response.status_code in {403, 404}:
            return None
        response.raise_for_status()
        return response

    def _article_url(self, pii: str) -> str:
        return f"{self.base_url}{ARTICLE_PII_PATH}{quote(pii.strip(), safe='')}"


class ScienceDirectSearchTool(SearchTool):
    """Execute Search V2 and enrich a bounded number of retained abstracts."""

    source_id = "sciencedirect"

    def __init__(
        self,
        transport: ResilientHttpClient,
        article_client: ScienceDirectArticleClient,
        *,
        base_url: str,
        headers: Mapping[str, str],
        abstract_enrichment_limit: int = 4,
    ) -> None:
        if abstract_enrichment_limit < 0:
            raise ValueError("abstract_enrichment_limit must be non-negative")
        self.transport = transport
        self.article_client = article_client
        self.base_url = base_url.rstrip("/")
        self.headers = dict(headers)
        self.abstract_enrichment_limit = abstract_enrichment_limit

    def search(self, query: str, *, limit: int = 10) -> Sequence[SearchHit]:
        if limit < 1:
            return ()
        page_size = _page_size(limit)
        response = self.transport.put(
            f"{self.base_url}{SEARCH_PATH}",
            headers={**self.headers, "Accept": "application/json"},
            json={
                "qs": query,
                "display": {"offset": 0, "show": page_size, "sortBy": "relevance"},
            },
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, Mapping):
            raise ValueError("ScienceDirect search response must be a JSON object")
        raw_results = payload.get("results", [])
        if not isinstance(raw_results, list):
            raise ValueError("ScienceDirect response field 'results' must be a list")
        hits = [_parse_search_result(item, base_url=self.base_url) for item in raw_results]
        retained = [hit for hit in hits if hit is not None][:limit]
        enriched: list[SearchHit] = []
        for index, hit in enumerate(retained):
            if index >= self.abstract_enrichment_limit:
                enriched.append(hit)
                continue
            try:
                metadata = self.article_client.fetch_metadata(hit.document_id)
            except (httpx.HTTPError, ProviderRequestError, ValueError) as exc:
                # Abstract enrichment is optional: a transport failure must not
                # discard already retrieved discovery metadata. Preserve a safe
                # machine-readable failure without URLs, headers or error bodies.
                response = getattr(exc, "response", None)
                hit = replace(hit, raw_metadata={
                    **hit.raw_metadata,
                    "abstract_enrichment": {
                        "status": "failed", "error_type": type(exc).__name__,
                        "transport_error_type": getattr(exc, "transport_error_type", None),
                        "http_status": (getattr(exc, "status_code", None)
                                        or getattr(response, "status_code", None)),
                    },
                })
                metadata = None
            enriched.append(_enrich_hit(hit, metadata))
        return enriched


class ScienceDirectFullTextTool(FullTextTool):
    source_id = "sciencedirect"

    def __init__(
        self,
        article_client: ScienceDirectArticleClient,
        *,
        max_chars: int = 100_000,
    ) -> None:
        if max_chars < 1:
            raise ValueError("max_chars must be positive")
        self.article_client = article_client
        self.max_chars = max_chars

    def fetch(self, document_id: str) -> FullText | None:
        response = self.article_client.fetch_full_text(document_id)
        if response is None or not response.text.strip():
            return None
        metadata = self.article_client.cached_metadata(document_id) or {}
        title = str(metadata.get("dc:title") or document_id).strip()
        text = response.text
        truncated = len(text) > self.max_chars
        return FullText(
            document_id=document_id,
            title=title,
            text=text[: self.max_chars],
            source=EvidenceSource(
                title=title,
                doi=_clean_doi(
                    metadata.get("prism:doi") or metadata.get("dc:identifier")
                ),
                url=_metadata_landing_url(metadata),
            ),
            media_type=response.headers.get("content-type", "text/plain").split(
                ";", 1
            )[0],
            content_extent="partial" if truncated else "full",
            source_url=str(response.url),
        )


def build_sciencedirect_source(config: Mapping[str, Any]) -> RetrievalSource:
    """Build a ScienceDirect capability bundle from non-secret provider config."""

    adapter = ScienceDirectQueryAdapter(
        max_query_chars=int(config.get("max_query_chars", 250))
    )
    if not config.get("enabled", False) or config.get("adapter_only", False):
        return RetrievalSource(source_id="sciencedirect", query_adapter=adapter)

    api_key = resolve_env_credential(
        config,
        setting="api_key_env",
        default_env="ELSEVIER_API_KEY",
    )
    institution_token = resolve_env_credential(
        config,
        setting="institution_token_env",
        default_env="ELSEVIER_INST_TOKEN",
        required=False,
    )
    headers = {"X-ELS-APIKey": api_key}
    if institution_token:
        headers["X-ELS-Insttoken"] = institution_token

    base_url = str(config.get("base_url", DEFAULT_BASE_URL)).rstrip("/")
    if not base_url.startswith(("https://", "http://")):
        raise ProviderConfigurationError("ScienceDirect base_url must be HTTP(S)")
    timeout = float(config.get("timeout_seconds", 30.0))
    max_retries = int(config.get("max_retries", 2))
    retry_backoff = float(config.get("retry_backoff_seconds", 0.5))
    client = httpx.Client(timeout=timeout, follow_redirects=True)
    search_transport = ResilientHttpClient(
        client,
        policy=HttpRequestPolicy(
            min_interval_seconds=float(
                config.get("search_min_interval_seconds", 0.5)
            ),
            max_retries=max_retries,
            retry_backoff_seconds=retry_backoff,
        ),
    )
    article_transport = ResilientHttpClient(
        client,
        policy=HttpRequestPolicy(
            min_interval_seconds=float(
                config.get("article_min_interval_seconds", 0.1)
            ),
            max_retries=max_retries,
            retry_backoff_seconds=retry_backoff,
        ),
    )
    article_client = ScienceDirectArticleClient(
        article_transport, base_url=base_url, headers=headers
    )
    return RetrievalSource(
        source_id="sciencedirect",
        query_adapter=adapter,
        search_tool=ScienceDirectSearchTool(
            search_transport,
            article_client,
            base_url=base_url,
            headers=headers,
            abstract_enrichment_limit=int(
                config.get("abstract_enrichment_limit", 4)
            ),
        ),
        full_text_tool=ScienceDirectFullTextTool(
            article_client,
            max_chars=int(config.get("full_text_max_chars", 100_000)),
        ),
        # Search V2 already supplies identity metadata. Abstract enrichment is
        # intentionally bounded inside the provider so the generic metadata
        # pass cannot turn it into an unbounded N+1 request pattern.
        metadata_tool=None,
    )


def _normalized_unique(values: Sequence[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        normalized = " ".join(value.split())
        if normalized and normalized not in result:
            result.append(normalized)
    return result


def _quote_query_term(term: str) -> str:
    escaped = term.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _page_size(limit: int) -> int:
    for size in _VALID_PAGE_SIZES:
        if limit <= size:
            return size
    return _VALID_PAGE_SIZES[-1]


def _parse_search_result(raw: Any, *, base_url: str) -> SearchHit | None:
    if not isinstance(raw, Mapping):
        return None
    pii = str(raw.get("pii") or "").strip()
    title = str(raw.get("title") or "").strip()
    if not pii or not title:
        return None
    raw_authors = raw.get("authors") or []
    authors = tuple(
        name
        for item in raw_authors
        if isinstance(item, Mapping)
        for name in [str(item.get("name") or "").strip()]
        if name
    )
    publication_date = str(raw.get("publicationDate") or "")
    year = (
        int(publication_date[:4])
        if len(publication_date) >= 4 and publication_date[:4].isdigit()
        else None
    )
    return SearchHit(
        document_id=pii,
        external_id=pii,
        title=title,
        authors=authors,
        year=year,
        doi=_clean_doi(raw.get("doi")),
        url=str(raw.get("uri") or "").strip() or None,
        full_text_url=(
            f"{base_url.rstrip('/')}{ARTICLE_PII_PATH}{quote(pii, safe='')}"
        ),
        source_id="sciencedirect",
        raw_metadata=dict(raw),
    )


def _enrich_hit(hit: SearchHit, metadata: Mapping[str, Any] | None) -> SearchHit:
    if not metadata:
        return hit
    abstract = str(metadata.get("dc:description") or "").strip()
    return replace(
        hit,
        abstract=abstract or hit.abstract,
        title=str(metadata.get("dc:title") or hit.title).strip(),
        doi=_clean_doi(
            metadata.get("prism:doi") or metadata.get("dc:identifier")
        )
        or hit.doi,
        url=_metadata_landing_url(metadata) or hit.url,
        raw_metadata={**hit.raw_metadata, "article_coredata": dict(metadata)},
    )


def _clean_doi(value: Any) -> str | None:
    normalized = str(value or "").strip()
    if normalized.lower().startswith("doi:"):
        normalized = normalized[4:]
    return normalized or None


def _metadata_landing_url(metadata: Mapping[str, Any]) -> str | None:
    links = metadata.get("link") or []
    if isinstance(links, Mapping):
        links = [links]
    if isinstance(links, list):
        for item in links:
            if isinstance(item, Mapping) and item.get("@rel") == "scidir":
                value = str(item.get("@href") or "").strip()
                if value:
                    return value
    value = str(metadata.get("prism:url") or "").strip()
    return value or None
