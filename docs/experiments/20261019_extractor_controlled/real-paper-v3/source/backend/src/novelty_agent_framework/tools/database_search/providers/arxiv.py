"""arXiv 数据源工具：检索、全文获取与元数据核验。

三个工具共用同一套 document_id 约定：arXiv ID 去掉 ``v<n>`` 版本后缀，
保证 SearchTool / FullTextTool / MetadataTool 之间标识自洽。
"""

from __future__ import annotations

import re
import time  # compatibility: tests patch the shared stdlib clock through this module
import xml.etree.ElementTree as ET
from collections.abc import Mapping, Sequence
from typing import Any
from html.parser import HTMLParser

import httpx
import pymupdf

from ....ports import FullText, FullTextTool, MetadataTool, SearchHit, SearchTool
from ....schemas import (
    EvidenceSource,
    ExternalIdentifier,
    ParsedCitation,
    SearchConcept,
)
from ..adapter import QueryAdapter, QueryAdapterError
from ..retrieval_sources import RetrievalSource
from .arxiv_identifiers import atom_identifier
from .arxiv_scheduler import (
    ARXIV_QUERY_URL,
    ArxivCircuitOpenError,
    ArxivRequestScheduler,
    ArxivResponseParseError,
    ArxivRetryBudgetExceeded,
    get_shared_arxiv_scheduler,
    reset_shared_arxiv_scheduler,
    retry_delay as _retry_delay,
)

from .arxiv_web import (
    ArxivWebChannelError,
    ArxivWebMetadataTool,
    ArxivWebSearchTool,
    build_arxiv_web_metadata_tool,
    build_arxiv_web_search_tool,
    build_arxiv_web_session,
)

ARXIV_ABS_URL = "https://arxiv.org/abs/"
ARXIV_HTML_URL = "https://arxiv.org/html/"
ARXIV_PDF_URL = "https://arxiv.org/pdf/"

ATOM_NS = "{http://www.w3.org/2005/Atom}"
ARXIV_NS = "{http://arxiv.org/schemas/atom}"

_VERSION_RE = re.compile(r"v\d+$")
# Deprecated compatibility sentinel; scheduling state now lives in the scheduler.
_LAST_REQUEST_AT = 0.0

DEFAULT_SEARCH_TRANSPORT = "web"
SEARCH_TRANSPORTS = ("api", "web")


def resolve_search_transport(config: Mapping[str, Any] | None = None) -> str:
    """读取并校验 ``search_transport``。

    取值非法时直接抛错而不是静默退回 API —— 打错一个字母就悄悄走另一条通道，
    比启动即失败更难排查。
    """

    raw = (
        str((config or {}).get("search_transport", DEFAULT_SEARCH_TRANSPORT))
        .strip()
        .lower()
    )
    if raw not in SEARCH_TRANSPORTS:
        raise ValueError(
            f"未知的 arxiv search_transport：{raw!r}；"
            f"可选：{', '.join(SEARCH_TRANSPORTS)}"
        )
    return raw


def search_transport_is_web(config: Mapping[str, Any] | None = None) -> bool:
    return resolve_search_transport(config) == "web"


class ArxivQueryAdapter(QueryAdapter):
    """把通用检索 Concept 编译为 arXiv ``all:`` 查询。"""

    database = "arxiv"

    def __init__(self, *, render_v2: bool = True, phrase_max_words: int = 3) -> None:
        self.render_v2 = render_v2
        self.phrase_max_words = phrase_max_words

    def _render_concept(
        self,
        concept: SearchConcept,
        *,
        use_alias: bool = False,
        use_exclude: bool = True,
    ) -> str:
        terms: list[str] = []
        seen: set[str] = set()
        for raw_term in concept.terms:
            term = " ".join(raw_term.split())
            if not term:
                raise QueryAdapterError(f"Concept {concept.concept_id} 包含空 term")
            if term not in seen:
                seen.add(term)
                terms.append(term)
        field = _field_for_role(concept.role)
        rendered = [
            render_arxiv_term(
                term,
                render_v2=self.render_v2,
                phrase_max_words=self.phrase_max_words,
                field=field,
            )
            for term in terms
        ]
        if use_alias:
            for raw_alias in concept.alias:
                alias = " ".join(raw_alias.split())
                if alias and alias not in seen:
                    seen.add(alias)
                    rendered.append(
                        render_arxiv_term(
                            alias,
                            render_v2=self.render_v2,
                            phrase_max_words=self.phrase_max_words,
                            field=field,
                        )
                    )
        block = rendered[0] if len(rendered) == 1 else f"({' OR '.join(rendered)})"
        excluded = [
            render_arxiv_term(
                " ".join(raw.split()),
                render_v2=self.render_v2,
                phrase_max_words=self.phrase_max_words,
                field="all",
            )
            for raw in concept.exclude
            if raw.strip()
        ]
        if use_exclude and excluded:
            block = f"{block} ANDNOT ({' OR '.join(excluded)})"
        return block


def _field_for_role(role: str | None) -> str:
    """按概念角色选择 arXiv 字段：object 标题优先，方法/特征用摘要，
    escape 用全字段，缺省（v1 兼容）用 all。
    """

    if role == "escape":
        return "all"
    if role == "object":
        return "ti"
    if role in ("method", "feature", "setting"):
        return "abs"
    return "all"


def render_arxiv_term(
    term: str,
    *,
    render_v2: bool = True,
    phrase_max_words: int = 3,
    field: str = "all",
) -> str:
    """把单个术语渲染为 arXiv all: 查询片段（纯函数，可单测）。

    - render_v2=False：始终 quoted phrase（all:"term"，v1 行为）；
    - render_v2=True：词数 <= phrase_max_words 保持 quoted phrase；
      词数更多时拆成词级 AND（all:w1 AND all:w2 ...），词级模式会
      剥离术语内嵌的双引号（引号只在短语内有意义）。
    """

    normalized = " ".join(term.split())
    if not normalized:
        raise ValueError("term is empty")
    if not render_v2:
        return f'{field}:"{_escape_query_term(normalized)}"'
    words = [token.strip('"') for token in normalized.split() if token.strip('"')]
    if len(words) <= phrase_max_words:
        return f'{field}:"{_escape_query_term(normalized)}"'
    return " AND ".join(f"{field}:{_escape_query_term(word)}" for word in words)


def _escape_query_term(term: str) -> str:
    return term.replace("\\", "\\\\").replace('"', '\\"')


def strip_version(arxiv_id: str) -> str:
    """去掉 arXiv ID 的版本后缀，如 ``2305.12345v2`` -> ``2305.12345``。"""

    return _VERSION_RE.sub("", arxiv_id)


def _clean_title(title: str) -> str:
    return re.sub(r"\s+", " ", title).strip()


def parse_entry(entry: ET.Element) -> SearchHit:
    """把 Atom entry 映射为 SearchHit。"""

    entry_id = entry.findtext(f"{ATOM_NS}id") or ""
    external_id = atom_identifier(entry_id)
    arxiv_id = strip_version(external_id)
    authors = tuple(
        name.text.strip()
        for name in entry.findall(f"{ATOM_NS}author/{ATOM_NS}name")
        if name.text and name.text.strip()
    )
    published = entry.findtext(f"{ATOM_NS}published") or ""
    return SearchHit(
        document_id=arxiv_id,
        title=_clean_title(entry.findtext(f"{ATOM_NS}title") or ""),
        abstract=(entry.findtext(f"{ATOM_NS}summary") or "").strip(),
        authors=authors,
        year=int(published[:4]) if len(published) >= 4 else None,
        doi=entry.findtext(f"{ARXIV_NS}doi"),
        url=f"{ARXIV_ABS_URL}{arxiv_id}",
        source_id="arxiv",
        external_id=external_id,
        full_text_url=f"{ARXIV_PDF_URL}{arxiv_id}",
        raw_metadata={"atom_id": entry_id, "published": published},
    )


class ArxivSearchTool(SearchTool):
    """arXiv 检索；所有 export API I/O 委托给进程级 scheduler。"""

    source_id = "arxiv"

    def __init__(
        self,
        *,
        client: httpx.Client | None = None,
        base_url: str = ARXIV_QUERY_URL,
        min_interval: float = 4.0,
        timeout: float = 20.0,
        max_retries: int = 2,
        max_retry_delay: float = 5.0,
        retry_budget_seconds: float = 45.0,
        circuit_failure_threshold: int = 2,
        circuit_cooldown_seconds: float = 60.0,
        scheduler: ArxivRequestScheduler | None = None,
        scheduler_enabled: bool = True,
        metadata_batch_enabled: bool = True,
        metadata_batch_window_ms: int = 200,
        metadata_batch_max_size: int = 32,
    ) -> None:
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        if min_interval < 0 or max_retries < 0 or max_retry_delay < 0:
            raise ValueError("retry and throttle values must be non-negative")
        if retry_budget_seconds <= 0:
            raise ValueError("retry_budget_seconds must be positive")
        if circuit_failure_threshold < 1 or circuit_cooldown_seconds < 0:
            raise ValueError("circuit breaker values are invalid")
        self._base_url = base_url
        self._min_interval = min_interval
        self._timeout = timeout
        self._max_retries = max_retries
        self._max_retry_delay = max_retry_delay
        self._retry_budget_seconds = retry_budget_seconds
        self._circuit_failure_threshold = circuit_failure_threshold
        self._circuit_cooldown_seconds = circuit_cooldown_seconds
        self._scheduler = scheduler or get_shared_arxiv_scheduler(
            client=client,
            base_url=base_url,
            min_interval=min_interval,
            timeout=timeout,
            max_retries=max_retries,
            max_retry_delay=max_retry_delay,
            retry_budget_seconds=retry_budget_seconds,
            circuit_failure_threshold=circuit_failure_threshold,
            circuit_cooldown_seconds=circuit_cooldown_seconds,
            scheduler_enabled=scheduler_enabled,
            metadata_batch_enabled=metadata_batch_enabled,
            metadata_batch_window_ms=metadata_batch_window_ms,
            metadata_batch_max_size=metadata_batch_max_size,
        )

    def search(self, query: str, *, limit: int = 10) -> Sequence[SearchHit]:
        response = self._scheduler.search(query, limit=limit)
        try:
            root = ET.fromstring(response.text)
        except ET.ParseError as exc:
            raise ArxivResponseParseError("invalid arXiv Atom response") from exc
        return [parse_entry(entry) for entry in root.findall(f"{ATOM_NS}entry")]

    def resolve_identifier(self, identifier: ExternalIdentifier) -> SearchHit | None:
        """按精确 arXiv ID 解析单篇文献；非 arxiv 命名空间返回 None。"""

        if identifier.namespace != "arxiv":
            return None
        hits = self.search(f"id:{strip_version(identifier.value)}", limit=2)
        target = strip_version(identifier.value).casefold()
        return next(
            (hit for hit in hits if strip_version(hit.document_id).casefold() == target),
            None,
        )

    def search_known_item(
        self, citation: ParsedCitation, *, limit: int = 5
    ) -> Sequence[SearchHit]:
        """按引文已知项检索：优先精确 arXiv ID，否则退回标题检索。"""

        if citation.arxiv_id:
            hit = self.resolve_identifier(
                ExternalIdentifier(namespace="arxiv", value=citation.arxiv_id)
            )
            return [hit] if hit else []
        if not citation.title:
            return []
        escaped = _escape_query_term(citation.title)
        return self.search(f'ti:"{escaped}"', limit=limit)

    def _get(self, url: str) -> httpx.Response:
        return self._scheduler.request_url(url)


class _HTMLTextExtractor(HTMLParser):
    """剥离 script/style 并保留正文文本的简易 HTML 解析器。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._skip_depth = 0
        self._parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in ("script", "style"):
            self._skip_depth += 1
        elif tag in ("p", "section", "div", "li", "br", "h1", "h2", "h3", "h4"):
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style") and self._skip_depth:
            self._skip_depth -= 1
        elif tag in ("p", "section", "div", "li", "h1", "h2", "h3", "h4"):
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip_depth:
            self._parts.append(data)

    def text(self) -> str:
        raw = "".join(self._parts)
        return re.sub(r"\n{3,}", "\n\n", re.sub(r"[ \t]+\n", "\n", raw)).strip()


class ArxivFullTextTool(FullTextTool):
    """arXiv 全文获取：HTML 优先，PDF（PyMuPDF）兜底，失败返回 None。"""

    source_id = "arxiv"

    def __init__(
        self,
        *,
        client: httpx.Client | None = None,
        timeout: float = 30.0,
        max_chars: int = 100_000,
    ) -> None:
        self._client = client or httpx.Client(timeout=timeout, follow_redirects=True)
        self._max_chars = max_chars
        self._cache: dict[str, FullText | None] = {}

    def fetch(self, document_id: str) -> FullText | None:
        doc_id = strip_version(document_id)
        if doc_id in self._cache:
            return self._cache[doc_id]
        result = self._fetch_html(doc_id) or self._fetch_pdf(doc_id)
        self._cache[doc_id] = result
        return result

    def _fetch_html(self, doc_id: str) -> FullText | None:
        response = self._try_get(f"{ARXIV_HTML_URL}{doc_id}")
        if response is None:
            return None
        extractor = _HTMLTextExtractor()
        try:
            extractor.feed(response.text)
            text = extractor.text()
        except Exception:
            return None
        if not text:
            return None
        title = self._html_title(response.text) or doc_id
        return self._make_full_text(
            doc_id, title, text, source_url=f"{ARXIV_HTML_URL}{doc_id}"
        )

    def _fetch_pdf(self, doc_id: str) -> FullText | None:
        response = self._try_get(f"{ARXIV_PDF_URL}{doc_id}")
        if response is None:
            return None
        try:
            document = pymupdf.open(stream=response.content, filetype="pdf")
        except Exception:
            return None
        try:
            text = "\n".join(page.get_text() for page in document)
        finally:
            document.close()
        if not text.strip():
            return None
        return self._make_full_text(
            doc_id, doc_id, text, source_url=f"{ARXIV_PDF_URL}{doc_id}"
        )

    def _make_full_text(
        self, doc_id: str, title: str, text: str, *, source_url: str
    ) -> FullText:
        truncated = len(text) > self._max_chars
        return FullText(
            document_id=doc_id,
            title=title,
            text=text[: self._max_chars],
            source=EvidenceSource(title=title, url=f"{ARXIV_ABS_URL}{doc_id}"),
            media_type="text/plain",
            content_extent="partial" if truncated else "unknown",
            source_url=source_url,
        )

    def _try_get(self, url: str) -> httpx.Response | None:
        try:
            from ....core.runtime_artifacts import current_runtime_artifacts
            runtime = current_runtime_artifacts()
            if runtime is not None:
                runtime.reserve_provider_request(provider="arxiv_auxiliary",
                                                 operation="GET")
            response = self._client.get(url)
            response.raise_for_status()
            return response
        except (httpx.HTTPError, ArxivWebChannelError):
            return None

    @staticmethod
    def _html_title(text: str) -> str | None:
        match = re.search(r"<title[^>]*>(.*?)</title>", text, re.DOTALL | re.IGNORECASE)
        if match is None:
            return None
        title = re.sub(r"<[^>]+>", "", match.group(1))
        return _clean_title(title) or None


class ArxivMetadataTool(MetadataTool):
    """arXiv 元数据核验：用 id_list 精确查询，返回规范 EvidenceSource。"""

    source_id = "arxiv"

    def __init__(
        self,
        *,
        client: httpx.Client | None = None,
        base_url: str = ARXIV_QUERY_URL,
        timeout: float = 20.0,
        scheduler: ArxivRequestScheduler | None = None,
        **scheduler_options: Any,
    ) -> None:
        self._scheduler = scheduler or get_shared_arxiv_scheduler(
            client=client, base_url=base_url, timeout=timeout, **scheduler_options
        )
        self._cache: dict[str, EvidenceSource | None] = {}

    def resolve(self, document_id: str) -> EvidenceSource | None:
        doc_id = strip_version(document_id)
        if doc_id in self._cache:
            return self._cache[doc_id]
        result = self._resolve_live(doc_id)
        self._cache[doc_id] = result
        return result

    def _resolve_live(self, doc_id: str) -> EvidenceSource | None:
        entry_xml = self._scheduler.resolve_metadata(doc_id)
        if entry_xml is None:
            return None
        try:
            entry = ET.fromstring(entry_xml)
        except ET.ParseError as exc:
            raise ArxivResponseParseError("invalid arXiv Atom entry") from exc
        hit = parse_entry(entry)
        return EvidenceSource(title=hit.title, doi=hit.doi, url=hit.url)


def build_arxiv_source(config: Mapping[str, Any]) -> RetrievalSource:
    """从来源专用配置构建自洽的 arXiv 能力包。"""

    enabled = bool(config.get("enabled", False))
    render_v2 = bool(config.get("render_v2", True))
    if not enabled or config.get("adapter_only", False):
        return RetrievalSource(
            source_id="arxiv",
            query_adapter=ArxivQueryAdapter(render_v2=render_v2),
        )
    timeout_seconds = float(config["timeout_seconds"])
    if search_transport_is_web(config):
        session = build_arxiv_web_session(config)
        full_text_tool = ArxivFullTextTool(client=session, max_chars=int(config["full_text_max_chars"]))
        search_tool = build_arxiv_web_search_tool(config, session=session)
        return RetrievalSource(
            source_id="arxiv",
            query_adapter=ArxivQueryAdapter(render_v2=render_v2),
            search_tool=search_tool,
            full_text_tool=full_text_tool,
            # 复用检索工具的 /abs/ 缓存：同一 work_id 先被检索链解析、再被元数据
            # 核验时不再重复打主站。
            metadata_tool=build_arxiv_web_metadata_tool(
                config, session=session, search=search_tool
            ),
        )

    full_text_client = httpx.Client(timeout=timeout_seconds, follow_redirects=True)
    full_text_tool = ArxivFullTextTool(
        client=full_text_client, max_chars=int(config["full_text_max_chars"])
    )
    scheduler = get_shared_arxiv_scheduler(
        min_interval=float(config.get("api_min_interval_seconds", config["min_interval_seconds"])),
        max_retries=int(config["max_retries"]),
        timeout=timeout_seconds,
        max_retry_delay=float(config.get("max_retry_delay_seconds", 5.0)),
        retry_budget_seconds=float(config.get("retry_budget_seconds", 45.0)),
        circuit_failure_threshold=int(config.get("circuit_failure_threshold", 2)),
        circuit_cooldown_seconds=float(config.get("circuit_cooldown_seconds", 60.0)),
        scheduler_enabled=bool(config.get("scheduler_enabled", True)),
        metadata_batch_enabled=bool(config.get("metadata_batch_enabled", True)),
        metadata_batch_window_ms=int(config.get("metadata_batch_window_ms", 200)),
        metadata_batch_max_size=int(config.get("metadata_batch_max_size", 32)),
    )
    return RetrievalSource(
        source_id="arxiv",
        query_adapter=ArxivQueryAdapter(render_v2=render_v2),
        search_tool=ArxivSearchTool(
            scheduler=scheduler,
            min_interval=float(config["min_interval_seconds"]),
            max_retries=int(config["max_retries"]),
            timeout=timeout_seconds,
            max_retry_delay=float(config.get("max_retry_delay_seconds", 5.0)),
            retry_budget_seconds=float(config.get("retry_budget_seconds", 45.0)),
            circuit_failure_threshold=int(
                config.get("circuit_failure_threshold", 2)
            ),
            circuit_cooldown_seconds=float(
                config.get("circuit_cooldown_seconds", 60.0)
            ),
        ),
        full_text_tool=full_text_tool,
        metadata_tool=ArxivMetadataTool(scheduler=scheduler),
    )


def build_arxiv_search_tool(options=None, *, client=None, scheduler=None) -> SearchTool:
    """Build the configured transport for bootstrap and provider callers."""
    config = dict(options or {})
    if search_transport_is_web(config):
        return build_arxiv_web_search_tool(config, session=build_arxiv_web_session(config, client=client))
    return ArxivSearchTool(
        client=client, scheduler=scheduler,
        min_interval=float(config.get("api_min_interval_seconds", config.get("min_interval_seconds", 4.0))),
        timeout=float(config.get("timeout_seconds", 20.0)),
        max_retries=int(config.get("max_retries", 2)),
        max_retry_delay=float(config.get("max_retry_delay_seconds", 5.0)),
        retry_budget_seconds=float(config.get("retry_budget_seconds", 45.0)),
        circuit_failure_threshold=int(config.get("circuit_failure_threshold", 2)),
        circuit_cooldown_seconds=float(config.get("circuit_cooldown_seconds", 60.0)),
    )
