"""两层检索：论文级种子检索（参考文献 + 英文关键词）与查新点级扩展检索。

规则版实现，不调用 LLM：检索词直接来自论文已有字段和查新点英文表述。
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from ..ports import SearchHit, SearchTool
from ..schemas import NoveltyPoint, PaperInput

_LEADING_NUM = re.compile(r"^\[?\d+\]?\s*")
_REF_MARKER = re.compile(r"\[[JDC]\]", re.IGNORECASE)
_TOKEN_RE = re.compile(r"[a-z0-9\u4e00-\u9fff]+")

_STOPWORDS = {
    "a", "an", "the", "of", "in", "on", "at", "for", "to", "and", "or",
    "with", "by", "using", "as", "from", "into", "through", "over",
    "that", "this", "these", "those", "is", "are", "be", "been", "was",
    "were", "can", "could", "will", "would", "should", "may", "might",
    "not", "no", "its", "it", "their", "our", "we", "they",
}

_SEED_CACHE: dict[str, list[SearchHit]] = {}


@dataclass(frozen=True)
class ExpansionResult:
    """扩展检索结果：去重后的命中集合 + 每条命中的查询命中数。"""

    hits: tuple[SearchHit, ...]
    hit_counts: dict[str, int]


@dataclass(frozen=True)
class RetrievalCandidate:
    """进入精读阶段的候选文献。"""

    hit: SearchHit
    cited_by_paper: bool
    query_hits: int
    score: float


def clear_seed_cache() -> None:
    _SEED_CACHE.clear()


def extract_reference_titles(
    references: Sequence[str],
    *,
    max_refs: int = 15,
) -> list[str]:
    """从参考文献字符串中规则提取候选标题（best-effort）。"""

    titles: list[str] = []
    for reference in references:
        title = _extract_reference_title(reference)
        if title and title not in titles:
            titles.append(title)
        if len(titles) >= max_refs:
            break
    return titles


def _extract_reference_title(reference: str) -> str | None:
    text = _LEADING_NUM.sub("", reference.strip())
    segments = [segment.strip() for segment in text.split(".")]
    for segment in segments:
        marker = _REF_MARKER.search(segment)
        if marker is not None:
            candidate = segment[: marker.start()].strip()
            if (
                8 <= len(candidate) <= 80
                and re.search(r"[A-Za-z]", candidate)
                and "http://" not in candidate
                and "https://" not in candidate
            ):
                return candidate
    for segment in segments:
        if 8 <= len(segment) <= 80 and re.search(r"[A-Za-z]", segment):
            return segment
    return None


def _tokens(text: str) -> set[str]:
    return set(_TOKEN_RE.findall(text.casefold()))


def title_similarity(left: str, right: str) -> float:
    """规范化后的 token Jaccard 相似度。"""

    left_tokens = _tokens(left)
    right_tokens = _tokens(right)
    if not left_tokens or not right_tokens:
        return 0.0
    return len(left_tokens & right_tokens) / len(left_tokens | right_tokens)


def search_reference_seed(
    paper: PaperInput,
    search_tool: SearchTool,
    *,
    cache: dict[str, list[SearchHit]] | None = None,
    max_refs: int = 15,
    title_threshold: float = 0.6,
    keyword_limit: int = 5,
) -> list[SearchHit]:
    """论文级种子检索：引用标题（ti: + 相似度校验）与英文关键词（all:）。"""

    store = cache if cache is not None else _SEED_CACHE
    if paper.paper_id in store:
        return list(store[paper.paper_id])

    hits: dict[str, SearchHit] = {}
    for title in extract_reference_titles(paper.references, max_refs=max_refs):
        try:
            results = search_tool.search(f'ti:"{title}"', limit=2)
        except Exception:
            continue
        for hit in results:
            if title_similarity(hit.title, title) >= title_threshold:
                hits.setdefault(hit.document_id, hit)

    for keyword in paper.keywords_en:
        try:
            results = search_tool.search(f'all:"{keyword}"', limit=keyword_limit)
        except Exception:
            continue
        for hit in results:
            hits.setdefault(hit.document_id, hit)

    seed = list(hits.values())
    store[paper.paper_id] = seed
    return list(seed)


def build_expansion_queries(point: NoveltyPoint) -> list[str]:
    """从查新点英文表述构造 arXiv 查询（规则版）。"""

    queries: list[str] = []
    claim = (point.claim_en or "").strip()
    features = [feature.strip() for feature in point.technical_features_en if feature.strip()]
    if claim and len(claim) <= 100:
        queries.append(f'abs:"{claim}"')
    if len(features) >= 2:
        queries.append(f'abs:"{features[0]}" AND abs:"{features[1]}"')
    for feature in features:
        if len(feature) <= 50:
            queries.append(f'abs:"{feature}"')
        else:
            words = _top_content_words(feature, limit=3)
            if words:
                queries.append(" AND ".join(f'all:"{word}"' for word in words))
    return queries


def _top_content_words(text: str, *, limit: int = 3) -> list[str]:
    """提取句子中最有区分度的内容词（按词长降序，去停用词）。"""

    words = [
        word
        for word in re.split(r"[^a-z0-9]+", text.casefold())
        if len(word) > 3 and word not in _STOPWORDS
    ]
    unique = list(dict.fromkeys(words))
    unique.sort(key=len, reverse=True)
    return unique[:limit]


def search_expansion(
    point: NoveltyPoint,
    search_tool: SearchTool,
    *,
    per_query_limit: int = 5,
) -> ExpansionResult:
    """查新点级扩展检索：执行查询、按 document_id 去重并统计命中数。"""

    hits: dict[str, SearchHit] = {}
    hit_counts: dict[str, int] = {}
    for query in build_expansion_queries(point):
        try:
            results = search_tool.search(query, limit=per_query_limit)
        except Exception:
            continue
        for hit in results:
            hits.setdefault(hit.document_id, hit)
            hit_counts[hit.document_id] = hit_counts.get(hit.document_id, 0) + 1
    return ExpansionResult(hits=tuple(hits.values()), hit_counts=hit_counts)


def merge_candidates(
    seed: Sequence[SearchHit],
    expansion: ExpansionResult,
    point: NoveltyPoint,
    *,
    top_n: int = 8,
    current_year: int | None = None,
) -> list[RetrievalCandidate]:
    """合并种子与扩展候选，标记引用归属，按规则打分并截断到 top_n。"""

    current_year = current_year or datetime.now(UTC).year
    seed_by_id = {hit.document_id: hit for hit in seed}
    point_text = " ".join(
        [point.claim_en or "", *point.technical_features_en]
    )

    seen: set[str] = set()
    candidates: list[RetrievalCandidate] = []
    for hit in [*seed, *expansion.hits]:
        if hit.document_id in seen:
            continue
        seen.add(hit.document_id)
        cited = hit.document_id in seed_by_id
        query_hits = max(1, expansion.hit_counts.get(hit.document_id, 1))
        overlap = _overlap(f"{hit.title} {hit.abstract}", point_text)
        recency = 0.5 if hit.year is not None and current_year - hit.year <= 3 else 0.0
        score = 2.0 * query_hits + overlap + recency
        candidates.append(
            RetrievalCandidate(
                hit=hit,
                cited_by_paper=cited,
                query_hits=query_hits,
                score=score,
            )
        )

    candidates.sort(key=lambda item: item.score, reverse=True)
    return candidates[:top_n]


def _overlap(candidate_text: str, point_text: str) -> float:
    left = _tokens(candidate_text)
    right = _tokens(point_text)
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)
