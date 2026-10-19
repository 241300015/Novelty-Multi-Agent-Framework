"""IEEE Xplore metadata search and Open Access full-text provider."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
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
    ResilientHttpClient,
    raise_for_provider_status,
    resolve_env_credential,
)
from .structured_text import markup_to_text, parse_xml_full_text

DEFAULT_BASE_URL = "https://ieeexploreapi.ieee.org/api/v1"
SEARCH_PATH = "/search/articles"
FULL_TEXT_PATH = "/search/document/{document_id}/fulltext"
_VALID_FULL_TEXT_MODES = frozenset({"disabled", "openaccess"})


class IEEEXploreQueryAdapter(QueryAdapter):
    """Compile a source-independent plan into Xplore ``querytext`` syntax."""

    database = "ieee_xplore"

    def __init__(self, *, max_query_chars: int = 2_000) -> None:
        if max_query_chars < 1:
            raise ValueError("max_query_chars must be positive")
        self.max_query_chars = max_query_chars

    def compile(self, plan: SearchPlan) -> Sequence[CompiledQuery]:
        compiled = list(super().compile(plan))
        for item in compiled:
            if len(item.query) > self.max_query_chars:
                raise QueryAdapterError(
                    "IEEE Xplore querytext exceeds "
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
        block = _render_terms(terms)
        excluded = _normalized_unique(concept.exclude) if use_exclude else []
        if excluded:
            block = f"{block} NOT {_render_terms(excluded)}"
        return block


class IEEEXploreArticleClient:
    """Retrieve only IEEE Open Access full text through the documented endpoint."""

    def __init__(
        self,
        transport: ResilientHttpClient,
        *,
        base_url: str,
        api_key: str,
        full_text_mode: str,
    ) -> None:
        if full_text_mode not in _VALID_FULL_TEXT_MODES:
            raise ProviderConfigurationError("invalid IEEE Xplore full-text mode")
        self.transport = transport
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.full_text_mode = full_text_mode
        self._records: dict[str, dict[str, Any]] = {}

    def remember(self, document_id: str, record: Mapping[str, Any]) -> None:
        self._records[document_id] = dict(record)

    def record(self, document_id: str) -> Mapping[str, Any]:
        return self._records.get(document_id, {})

    def fetch_full_text(self, document_id: str) -> httpx.Response | None:
        if self.full_text_mode == "disabled":
            return None
        record = self._records.get(document_id, {})
        if _open_access_state(record) is False:
            return None
        path = FULL_TEXT_PATH.format(document_id=quote(document_id.strip(), safe=""))
        response = self.transport.get(
            f"{self.base_url}{path}",
            params={"apikey": self.api_key, "format": "xml"},
            headers={"Accept": "application/xml"},
        )
        # IEEE returns 400 for some valid article numbers that are not available
        # through the Open Access endpoint.
        if response.status_code in {400, 403, 404}:
            return None
        raise_for_provider_status(response, provider="IEEE Xplore")
        return response

    def public_full_text_endpoint(self, document_id: str) -> str:
        path = FULL_TEXT_PATH.format(document_id=quote(document_id.strip(), safe=""))
        return f"{self.base_url}{path}"


class IEEEXploreSearchTool(SearchTool):
    source_id = "ieee_xplore"

    def __init__(
        self,
        transport: ResilientHttpClient,
        article_client: IEEEXploreArticleClient,
        *,
        base_url: str,
        api_key: str,
    ) -> None:
        self.transport = transport
        self.article_client = article_client
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key

    def search(self, query: str, *, limit: int = 10) -> Sequence[SearchHit]:
        if limit < 1:
            return ()
        response = self.transport.get(
            f"{self.base_url}{SEARCH_PATH}",
            params={
                "querytext": query,
                "max_records": min(limit, 200),
                "start_record": 1,
                "format": "json",
                "apikey": self.api_key,
            },
            headers={"Accept": "application/json"},
        )
        raise_for_provider_status(response, provider="IEEE Xplore")
        payload = response.json()
        if not isinstance(payload, Mapping):
            raise ValueError("IEEE Xplore response must be a JSON object")
        articles = payload.get("articles", [])
        if not isinstance(articles, list):
            raise ValueError("IEEE Xplore response field 'articles' must be a list")
        hits: list[SearchHit] = []
        for raw in articles:
            hit = _parse_search_record(raw)
            if hit is None:
                continue
            self.article_client.remember(hit.document_id, raw)
            hits.append(hit)
            if len(hits) >= limit:
                break
        return hits


class IEEEXploreFullTextTool(FullTextTool):
    source_id = "ieee_xplore"

    def __init__(
        self,
        article_client: IEEEXploreArticleClient,
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
        record = self.article_client.record(document_id)
        fallback_title = _clean_text(record.get("title")) or document_id
        parsed = parse_xml_full_text(response.text, fallback_title=fallback_title)
        if parsed is None:
            return None
        text = parsed.text
        truncated = len(text) > self.max_chars
        doi = _clean_doi(record.get("doi"))
        landing_url = _clean_text(record.get("html_url")) or None
        return FullText(
            document_id=document_id,
            title=parsed.title,
            text=text[: self.max_chars],
            source=EvidenceSource(
                title=parsed.title,
                doi=doi,
                url=landing_url or (f"https://doi.org/{doi}" if doi else None),
            ),
            sections=parsed.sections,
            media_type=response.headers.get("content-type", "application/xml").split(
                ";", 1
            )[0],
            content_extent="partial" if truncated else "full",
            source_url=(
                landing_url or self.article_client.public_full_text_endpoint(document_id)
            ),
        )


def build_ieee_xplore_source(config: Mapping[str, Any]) -> RetrievalSource:
    """Build IEEE metadata search and optional Open Access full text."""

    adapter = IEEEXploreQueryAdapter(
        max_query_chars=int(config.get("max_query_chars", 2_000))
    )
    if not config.get("enabled", False) or config.get("adapter_only", False):
        return RetrievalSource(source_id="ieee_xplore", query_adapter=adapter)

    api_key = resolve_env_credential(
        config, setting="api_key_env", default_env="IEEE_XPLORE_API_KEY"
    )
    assert api_key is not None
    full_text_mode = str(config.get("full_text_mode", "openaccess")).strip().lower()
    if full_text_mode not in _VALID_FULL_TEXT_MODES:
        raise ProviderConfigurationError(
            "IEEE Xplore full_text_mode must be disabled or openaccess; "
            "chargeable full text requires a separately contracted token flow"
        )
    base_url = str(config.get("base_url", DEFAULT_BASE_URL)).rstrip("/")
    if not base_url.startswith(("https://", "http://")):
        raise ProviderConfigurationError("IEEE Xplore base_url must be HTTP(S)")
    transport = ResilientHttpClient(
        httpx.Client(
            timeout=float(config.get("timeout_seconds", 30.0)),
            follow_redirects=True,
        ),
        policy=HttpRequestPolicy(
            min_interval_seconds=float(config.get("min_interval_seconds", 0.2)),
            max_retries=int(config.get("max_retries", 2)),
            retry_backoff_seconds=float(config.get("retry_backoff_seconds", 0.5)),
        ),
    )
    article_client = IEEEXploreArticleClient(
        transport,
        base_url=base_url,
        api_key=api_key,
        full_text_mode=full_text_mode,
    )
    return RetrievalSource(
        source_id="ieee_xplore",
        query_adapter=adapter,
        search_tool=IEEEXploreSearchTool(
            transport, article_client, base_url=base_url, api_key=api_key
        ),
        full_text_tool=(
            None
            if full_text_mode == "disabled"
            else IEEEXploreFullTextTool(
                article_client,
                max_chars=int(config.get("full_text_max_chars", 100_000)),
            )
        ),
        metadata_tool=None,
    )


def _parse_search_record(raw: Any) -> SearchHit | None:
    if not isinstance(raw, Mapping):
        return None
    document_id = _clean_text(raw.get("article_number"))
    title = markup_to_text(raw.get("title"))
    if not document_id or not title:
        return None
    publication_date = _clean_text(
        raw.get("publication_year") or raw.get("publication_date")
    )
    authors_value = raw.get("authors") or []
    if isinstance(authors_value, Mapping):
        authors_value = authors_value.get("authors", authors_value)
    if isinstance(authors_value, Mapping):
        authors_value = [authors_value]
    authors = (
        tuple(
            name
            for item in authors_value
            for name in [_author_name(item)]
            if name
        )
        if isinstance(authors_value, list)
        else ()
    )
    doi = _clean_doi(raw.get("doi"))
    landing_url = _clean_text(raw.get("html_url")) or None
    pdf_url = _clean_text(raw.get("pdf_url")) or None
    return SearchHit(
        document_id=document_id,
        external_id=document_id,
        title=title,
        abstract=markup_to_text(raw.get("abstract")),
        authors=authors,
        year=_year_from(publication_date),
        doi=doi,
        url=landing_url or (f"https://doi.org/{doi}" if doi else None),
        full_text_url=pdf_url if _open_access_state(raw) is True else None,
        source_id="ieee_xplore",
        raw_metadata=dict(raw),
    )


def _author_name(item: Any) -> str:
    if isinstance(item, Mapping):
        return _clean_text(item.get("full_name") or item.get("name"))
    return _clean_text(item)


def _open_access_state(record: Mapping[str, Any]) -> bool | None:
    for key in ("is_open_access", "open_access", "access_type"):
        if key not in record:
            continue
        value = record[key]
        if isinstance(value, bool):
            return value
        normalized = re.sub(r"[^a-z0-9]", "", str(value).lower())
        if normalized in {"true", "yes", "1", "open", "openaccess"}:
            return True
        if normalized in {"false", "no", "0", "locked", "subscription"}:
            return False
    return None


def _clean_doi(value: Any) -> str | None:
    normalized = _clean_text(value)
    lowered = normalized.lower()
    for prefix in ("https://doi.org/", "http://doi.org/", "http://dx.doi.org/", "doi:"):
        if lowered.startswith(prefix):
            normalized = normalized[len(prefix) :]
            break
    return normalized or None


def _clean_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _year_from(value: str) -> int | None:
    match = re.search(r"(?<!\d)(19|20)\d{2}(?!\d)", value)
    return int(match.group()) if match else None


def _normalized_unique(values: Sequence[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        normalized = _clean_text(value)
        if normalized and normalized not in result:
            result.append(normalized)
    return result


def _render_terms(terms: Sequence[str]) -> str:
    rendered = [f'"{_escape_term(term)}"' for term in terms]
    return rendered[0] if len(rendered) == 1 else f"({' OR '.join(rendered)})"


def _escape_term(term: str) -> str:
    return term.replace("\\", "\\\\").replace('"', '\\"')
