"""Springer Nature Meta API and permission-aware JATS full-text provider."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

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

DEFAULT_BASE_URL = "https://api.springernature.com"
META_PATH = "/meta/v2/json"
OPEN_ACCESS_PATH = "/openaccess/jats"
TDM_PATH = "/xmldata/jats"
_VALID_FULL_TEXT_MODES = frozenset({"disabled", "openaccess", "tdm"})


class SpringerNatureQueryAdapter(QueryAdapter):
    """Compile a source-independent plan into Springer Nature query syntax."""

    database = "springer"

    def __init__(self, *, max_query_chars: int = 1_000) -> None:
        if max_query_chars < 1:
            raise ValueError("max_query_chars must be positive")
        self.max_query_chars = max_query_chars

    def compile(self, plan: SearchPlan) -> Sequence[CompiledQuery]:
        compiled = list(super().compile(plan))
        for item in compiled:
            if len(item.query) > self.max_query_chars:
                raise QueryAdapterError(
                    "Springer Nature query exceeds "
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


class SpringerNatureArticleClient:
    """Keep search metadata and retrieve an exact JATS record without key leakage."""

    def __init__(
        self,
        transport: ResilientHttpClient,
        *,
        base_url: str,
        meta_api_key: str,
        open_access_api_key: str | None,
        full_text_mode: str,
        tdm_api_metric: str | None = None,
    ) -> None:
        if full_text_mode not in _VALID_FULL_TEXT_MODES:
            raise ProviderConfigurationError("invalid Springer full-text mode")
        if full_text_mode == "openaccess" and not open_access_api_key:
            raise ProviderConfigurationError(
                "Springer Open Access mode requires its own API key"
            )
        if full_text_mode == "tdm" and not tdm_api_metric:
            raise ProviderConfigurationError("Springer TDM mode requires an API metric")
        self.transport = transport
        self.base_url = base_url.rstrip("/")
        self.meta_api_key = meta_api_key
        self.open_access_api_key = open_access_api_key
        self.full_text_mode = full_text_mode
        self.tdm_api_metric = tdm_api_metric
        self._records: dict[str, dict[str, Any]] = {}

    def remember(self, document_id: str, record: Mapping[str, Any]) -> None:
        self._records[document_id] = dict(record)

    def fetch_full_text(self, document_id: str) -> httpx.Response | None:
        record = self._records.get(document_id, {})
        if self.full_text_mode == "disabled":
            return None
        if self.full_text_mode == "openaccess" and record and not _is_open_access(record):
            return None
        doi = _record_doi(record) or _doi_from_identifier(document_id)
        if not doi:
            return None
        path = OPEN_ACCESS_PATH
        credential = self.open_access_api_key
        if self.full_text_mode == "tdm":
            path = TDM_PATH
            credential = f"{self.meta_api_key}/{self.tdm_api_metric}"
        assert credential is not None
        response = self.transport.get(
            f"{self.base_url}{path}",
            params={
                "q": f"doi:{doi}",
                "p": 1,
                "api_key": credential,
            },
            headers={"Accept": "application/xml"},
        )
        if response.status_code in {403, 404}:
            return None
        raise_for_provider_status(response, provider="Springer Nature")
        return response

    def record(self, document_id: str) -> Mapping[str, Any]:
        return self._records.get(document_id, {})

    @property
    def public_full_text_endpoint(self) -> str:
        path = TDM_PATH if self.full_text_mode == "tdm" else OPEN_ACCESS_PATH
        return f"{self.base_url}{path}"


class SpringerNatureSearchTool(SearchTool):
    source_id = "springer"

    def __init__(
        self,
        transport: ResilientHttpClient,
        article_client: SpringerNatureArticleClient,
        *,
        base_url: str,
        meta_api_key: str,
    ) -> None:
        self.transport = transport
        self.article_client = article_client
        self.base_url = base_url.rstrip("/")
        self.meta_api_key = meta_api_key

    def search(self, query: str, *, limit: int = 10) -> Sequence[SearchHit]:
        if limit < 1:
            return ()
        response = self.transport.get(
            f"{self.base_url}{META_PATH}",
            params={
                "q": query,
                "s": 1,
                "p": min(limit, 100),
                "api_key": self.meta_api_key,
            },
            headers={"Accept": "application/json"},
        )
        # Meta API uses this specific 404 body for an exhausted query. A plain
        # 404 (missing endpoint/resource) must remain a technical failure.
        if _is_meta_no_match_response(response):
            return ()
        raise_for_provider_status(response, provider="Springer Nature")
        payload = response.json()
        if not isinstance(payload, Mapping):
            raise ValueError("Springer Nature response must be a JSON object")
        records = payload.get("records", [])
        if not isinstance(records, list):
            raise ValueError("Springer Nature response field 'records' must be a list")
        hits: list[SearchHit] = []
        for raw in records:
            hit = _parse_search_record(raw)
            if hit is None:
                continue
            self.article_client.remember(hit.document_id, raw)
            hits.append(hit)
            if len(hits) >= limit:
                break
        return hits


def _is_meta_no_match_response(response: httpx.Response) -> bool:
    """Recognize only the no-data contract confirmed in live Meta API traces."""
    if response.status_code != 404:
        return False
    try:
        payload = response.json()
    except ValueError:
        return False
    if not isinstance(payload, Mapping):
        return False
    error = payload.get("error")
    return (
        payload.get("status") == "Fail"
        and payload.get("message") == "No data was found for the given query."
        and payload.get("records") in (None, [])
        and isinstance(error, Mapping)
        and error.get("error") == "Not Found"
        and error.get("error_description")
        == "No matching data is available for the requested query."
    )


class SpringerNatureFullTextTool(FullTextTool):
    source_id = "springer"

    def __init__(
        self,
        article_client: SpringerNatureArticleClient,
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
        fallback_title = str(record.get("title") or document_id).strip()
        parsed = parse_xml_full_text(response.text, fallback_title=fallback_title)
        if parsed is None:
            return None
        text = parsed.text
        truncated = len(text) > self.max_chars
        doi = _record_doi(record) or _doi_from_identifier(document_id)
        landing_url, _ = _record_urls(record)
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
                landing_url
                or (f"https://doi.org/{doi}" if doi else None)
                or self.article_client.public_full_text_endpoint
            ),
        )


def build_springer_source(config: Mapping[str, Any]) -> RetrievalSource:
    """Build Springer metadata plus optional OA/TDM full-text capabilities."""

    adapter = SpringerNatureQueryAdapter(
        max_query_chars=int(config.get("max_query_chars", 1_000))
    )
    if not config.get("enabled", False) or config.get("adapter_only", False):
        return RetrievalSource(source_id="springer", query_adapter=adapter)

    meta_api_key = resolve_env_credential(
        config,
        setting="meta_api_key_env",
        default_env="SPRINGER_NATURE_META_API_KEY",
    )
    assert meta_api_key is not None
    full_text_mode = str(config.get("full_text_mode", "openaccess")).strip().lower()
    if full_text_mode not in _VALID_FULL_TEXT_MODES:
        raise ProviderConfigurationError(
            "Springer full_text_mode must be disabled, openaccess, or tdm"
        )
    open_access_api_key = resolve_env_credential(
        config,
        setting="open_access_api_key_env",
        default_env="SPRINGER_NATURE_OPEN_ACCESS_API_KEY",
        required=full_text_mode == "openaccess",
    )
    tdm_api_metric = None
    if full_text_mode == "tdm":
        tdm_api_metric = resolve_env_credential(
            config,
            setting="tdm_api_metric_env",
            default_env="SPRINGER_NATURE_TDM_API_METRIC",
        )

    base_url = str(config.get("base_url", DEFAULT_BASE_URL)).rstrip("/")
    _validate_base_url(base_url)
    policy = HttpRequestPolicy(
        min_interval_seconds=float(config.get("min_interval_seconds", 0.6)),
        max_retries=int(config.get("max_retries", 2)),
        retry_backoff_seconds=float(config.get("retry_backoff_seconds", 0.5)),
    )
    transport = ResilientHttpClient(
        httpx.Client(
            timeout=float(config.get("timeout_seconds", 30.0)),
            follow_redirects=True,
        ),
        policy=policy,
    )
    article_client = SpringerNatureArticleClient(
        transport,
        base_url=base_url,
        meta_api_key=meta_api_key,
        open_access_api_key=open_access_api_key,
        full_text_mode=full_text_mode,
        tdm_api_metric=tdm_api_metric,
    )
    return RetrievalSource(
        source_id="springer",
        query_adapter=adapter,
        search_tool=SpringerNatureSearchTool(
            transport,
            article_client,
            base_url=base_url,
            meta_api_key=meta_api_key,
        ),
        full_text_tool=(
            None
            if full_text_mode == "disabled"
            else SpringerNatureFullTextTool(
                article_client,
                max_chars=int(config.get("full_text_max_chars", 100_000)),
            )
        ),
        metadata_tool=None,
    )


def _parse_search_record(raw: Any) -> SearchHit | None:
    if not isinstance(raw, Mapping):
        return None
    title = _clean_text(raw.get("title"))
    identifier = _clean_text(raw.get("identifier"))
    doi = _record_doi(raw)
    document_id = doi or identifier
    if not title or not document_id:
        return None
    creators = raw.get("creators") or []
    if isinstance(creators, Mapping):
        creators = [creators]
    authors = (
        tuple(
            value
            for item in creators
            for value in [_creator_name(item)]
            if value
        )
        if isinstance(creators, list)
        else ()
    )
    publication_date = _clean_text(raw.get("publicationDate"))
    year = _year_from(publication_date)
    landing_url, pdf_url = _record_urls(raw)
    return SearchHit(
        document_id=document_id,
        external_id=identifier or document_id,
        title=title,
        abstract=markup_to_text(raw.get("abstract")),
        authors=authors,
        year=year,
        doi=doi,
        url=landing_url or (f"https://doi.org/{doi}" if doi else None),
        full_text_url=pdf_url if _is_open_access(raw) else None,
        source_id="springer",
        raw_metadata=dict(raw),
    )


def _record_urls(record: Mapping[str, Any]) -> tuple[str | None, str | None]:
    values = record.get("url") or []
    if isinstance(values, Mapping):
        values = [values]
    html_url: str | None = None
    pdf_url: str | None = None
    if isinstance(values, list):
        for item in values:
            if not isinstance(item, Mapping):
                continue
            value = _clean_text(item.get("value"))
            if not value:
                continue
            fmt = _clean_text(item.get("format")).lower()
            if fmt == "pdf" and pdf_url is None:
                pdf_url = value
            elif fmt == "html" and html_url is None:
                html_url = value
            elif html_url is None and "doi.org/" not in value:
                html_url = value
    return html_url, pdf_url


def _creator_name(item: Any) -> str:
    if isinstance(item, Mapping):
        return _clean_text(item.get("creator") or item.get("name"))
    return _clean_text(item)


def _is_open_access(record: Mapping[str, Any]) -> bool:
    value = record.get("openaccess", record.get("openAccess", False))
    return value is True or str(value).strip().lower() in {"true", "yes", "1"}


def _clean_doi(value: Any) -> str | None:
    normalized = _clean_text(value)
    lowered = normalized.lower()
    for prefix in ("https://doi.org/", "http://doi.org/", "http://dx.doi.org/", "doi:"):
        if lowered.startswith(prefix):
            normalized = normalized[len(prefix) :]
            break
    return normalized or None


def _record_doi(record: Mapping[str, Any]) -> str | None:
    explicit = _clean_doi(record.get("doi"))
    if explicit:
        return explicit
    return _doi_from_identifier(_clean_text(record.get("identifier")))


def _doi_from_identifier(value: str) -> str | None:
    lowered = value.lower()
    if lowered.startswith(
        ("doi:", "https://doi.org/", "http://doi.org/", "http://dx.doi.org/")
    ):
        return _clean_doi(value)
    return None


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


def _validate_base_url(value: str) -> None:
    if not value.startswith(("https://", "http://")):
        raise ProviderConfigurationError("Springer Nature base_url must be HTTP(S)")
