"""Deterministic citation resolution and subject-reference materialization."""

from __future__ import annotations

import asyncio
import hashlib
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from difflib import SequenceMatcher
from typing import Any, Iterable

from ..persistence import SubjectReferenceStore
from ..ports import SearchHit
from ..schemas import (
    Artifact,
    ArtifactRole,
    ContentExtent,
    ExternalIdentifier,
    ParsedCitation,
    ReferenceBootstrapEntry,
    ReferenceBootstrapManifest,
    ReferenceManifest,
    ReferenceResolveAttempt,
    ResolutionStatus,
)
from ..tools.database_search.structured_retrieval import (
    StructuredRetrievalAdapter,
    _invoke_provider,
)


class CitationParser:
    DOI = re.compile(r"(?i)\b(?:doi\s*:\s*|https?://doi\.org/)?(10\.\d{4,9}/[-._;()/:A-Z0-9]+)")
    ARXIV = re.compile(r"(?i)(?:arxiv\s*:\s*|arxiv\.org/(?:abs|pdf)/)([a-z.-]+/\d{7}|\d{4}\.\d{4,5})(?:v\d+)?")
    URL = re.compile(r"https?://[^\s<>\]\[\"']+")
    YEAR = re.compile(r"(?<!\d)((?:18|19|20)\d{2})(?!\d)")

    def parse(self, raw: str) -> ParsedCitation:
        text = " ".join(raw.split())
        doi_match = self.DOI.search(text)
        arxiv_match = self.ARXIV.search(text)
        url_match = self.URL.search(text)
        year_match = self.YEAR.search(text)
        doi = doi_match.group(1).rstrip(".,;)") if doi_match else None
        url = url_match.group(0).rstrip(".,;)") if url_match else None
        # A deliberately conservative title heuristic: quoted text first, then
        # the longest sentence-like segment after removing identifiers.
        quoted = re.search(r"[\"“](.{4,}?)[\"”]", text)
        segments = [part.strip(" .;,，。") for part in re.split(r"[。;]", text)]
        candidates = [part for part in segments if len(part) >= 8 and not self.URL.fullmatch(part)]
        title = quoted.group(1).strip() if quoted else (max(candidates, key=len) if candidates else None)
        if title == text and not (year_match or doi_match or arxiv_match or url_match):
            title = None
        authors: list[str] = []
        if title:
            prefix = text.split(title, 1)[0].strip(" []().,;，。").replace(" et al", "")
            if prefix and len(prefix) < 160:
                authors = [item.strip() for item in re.split(r",|，|\band\b|和", prefix) if item.strip()]
        warnings = [] if any((title, doi, arxiv_match, url)) else ["citation could not be structurally parsed"]
        return ParsedCitation(
            title=title,
            authors=authors,
            year=int(year_match.group(1)) if year_match else None,
            doi=doi.lower() if doi else None,
            url=url,
            arxiv_id=arxiv_match.group(1) if arxiv_match else None,
            warnings=warnings,
        )


@dataclass(frozen=True)
class CitationMatch:
    status: ResolutionStatus
    selected: SearchHit | None = None
    candidates: tuple[SearchHit, ...] = ()


class CitationMatcher:
    """One conservative identity policy shared by every provider."""

    def match(self, citation: ParsedCitation, hits: Iterable[SearchHit]) -> CitationMatch:
        unique = list({_hit_key(hit): hit for hit in hits}.values())
        if citation.doi:
            exact = [h for h in unique if h.doi and _doi(h.doi) == _doi(citation.doi)]
            if len(exact) == 1:
                return CitationMatch(ResolutionStatus.RESOLVED, exact[0], tuple(exact))
            if len(exact) > 1:
                return CitationMatch(ResolutionStatus.AMBIGUOUS, candidates=tuple(exact))
        if citation.arxiv_id:
            exact = [h for h in unique if _arxiv(h.document_id) == _arxiv(citation.arxiv_id) or _arxiv(h.external_id or "") == _arxiv(citation.arxiv_id)]
            if len(exact) == 1:
                return CitationMatch(ResolutionStatus.RESOLVED, exact[0], tuple(exact))
        if not citation.title:
            return CitationMatch(ResolutionStatus.NOT_FOUND, candidates=tuple(unique))
        scored = sorted(((self._score(citation, h), h) for h in unique), key=lambda x: x[0], reverse=True)
        plausible = [(score, hit) for score, hit in scored if score >= 0.82]
        if not plausible:
            return CitationMatch(ResolutionStatus.NOT_FOUND, candidates=tuple(unique))
        if len(plausible) > 1 and plausible[0][0] - plausible[1][0] < 0.06:
            return CitationMatch(ResolutionStatus.AMBIGUOUS, candidates=tuple(h for _, h in plausible))
        return CitationMatch(ResolutionStatus.RESOLVED, plausible[0][1], tuple(h for _, h in plausible))

    @staticmethod
    def _score(citation: ParsedCitation, hit: SearchHit) -> float:
        title_score = SequenceMatcher(None, _text(citation.title or ""), _text(hit.title)).ratio()
        if title_score == 1.0:
            return 1.0
        authors = {_text(a) for a in citation.authors if _text(a)}
        candidate_authors = {_text(a) for a in hit.authors if _text(a)}
        author_score = len(authors & candidate_authors) / len(authors) if authors else 0.5
        year_score = 0.5 if citation.year is None or hit.year is None else float(citation.year == hit.year)
        return 0.75 * title_score + 0.15 * author_score + 0.10 * year_score


class ReferenceProviderRegistry:
    def __init__(self, providers: Iterable[Any] = ()) -> None:
        self._providers: dict[str, Any] = {}
        for provider in providers:
            self.register(provider)

    def register(self, provider: Any) -> None:
        provider_id = str(getattr(provider, "source_id", getattr(provider, "provider_id", ""))).strip().lower()
        if not provider_id:
            raise ValueError("provider requires source_id or provider_id")
        if provider_id in self._providers:
            raise ValueError(f"duplicate provider {provider_id!r}")
        self._providers[provider_id] = provider

    def providers(self, only: str | None = None) -> list[tuple[str, Any]]:
        if only is None:
            return list(self._providers.items())
        key = only.strip().lower()
        return [(key, self._providers[key])] if key in self._providers else []


class ReferenceBootstrapService:
    def __init__(self, registry: ReferenceProviderRegistry, store: SubjectReferenceStore | None = None, *, matcher: CitationMatcher | None = None, parser: CitationParser | None = None, max_concurrency: int = 4) -> None:
        if max_concurrency < 1:
            raise ValueError("max_concurrency must be positive")
        self.registry, self.store = registry, store or SubjectReferenceStore()
        self.matcher, self.parser = matcher or CitationMatcher(), parser or CitationParser()
        self.max_concurrency = max_concurrency
        self.adapter = StructuredRetrievalAdapter()

    async def bootstrap(self, paper_id: str, references: list[str], *, force: bool = False, retry_failed: bool = False, provider: str | None = None, dry_run: bool = False) -> ReferenceBootstrapManifest:
        ledger = self.store.load_bootstrap(paper_id)
        existing = {entry.reference_id: entry for entry in ledger.entries}
        semaphore = asyncio.Semaphore(self.max_concurrency)

        async def run(ordinal: int, raw: str) -> ReferenceBootstrapEntry:
            reference_id = self.adapter.stable_id("ref", paper_id, str(ordinal), raw)
            old = existing.get(reference_id)
            if old and not force and not (retry_failed and old.resolution_status in {ResolutionStatus.FAILED, ResolutionStatus.NOT_FOUND}):
                return old
            async with semaphore:
                entry = await self._resolve_one(paper_id, reference_id, ordinal, raw, provider=provider, dry_run=dry_run)
                existing[reference_id] = entry
                current = ReferenceBootstrapManifest(
                    subject_paper_id=paper_id,
                    references_digest=references_digest(references),
                    entries=sorted(existing.values(), key=lambda x: x.ordinal),
                )
                self.store.persist_bootstrap(paper_id, current)
                return entry

        entries = await asyncio.gather(*(run(i, raw) for i, raw in enumerate(references, 1)))
        result = ReferenceBootstrapManifest(
            subject_paper_id=paper_id,
            references_digest=references_digest(references),
            entries=list(entries),
        )
        self.store.persist_bootstrap(paper_id, result)
        return result

    async def _resolve_one(self, paper_id: str, reference_id: str, ordinal: int, raw: str, *, provider: str | None, dry_run: bool) -> ReferenceBootstrapEntry:
        parsed = self.parser.parse(raw)
        attempts: list[ReferenceResolveAttempt] = []
        providers = self.registry.providers(provider)
        if dry_run:
            attempts.append(self._attempt(reference_id, "bootstrap", "dry_run", raw, "planned"))
            return ReferenceBootstrapEntry(reference_id=reference_id, ordinal=ordinal, raw_reference=raw, parsed=parsed, resolution_status=ResolutionStatus.NOT_FOUND, attempts=attempts)
        try:
            for namespace, value in (("doi", parsed.doi), ("arxiv", parsed.arxiv_id), ("url", parsed.url)):
                if not value:
                    continue
                for provider_id, capability in providers:
                    resolver = getattr(capability, "resolve_identifier", None)
                    if resolver is None:
                        continue
                    try:
                        hit = await _invoke_provider(
                            resolver,
                            ExternalIdentifier(namespace=namespace, value=value),
                        )
                    except Exception as exc:
                        attempts.append(self._attempt(reference_id, provider_id, f"{namespace}_exact", value, "failed", error=f"{type(exc).__name__}: {exc}"[:1000]))
                        continue
                    hits = [hit] if hit else []
                    match = self.matcher.match(parsed, hits)
                    attempts.append(self._attempt(reference_id, provider_id, f"{namespace}_exact", value, match.status.value, hits))
                    if match.status == ResolutionStatus.RESOLVED:
                        return self._materialize(reference_id, ordinal, raw, parsed, attempts, provider_id, match.selected, paper_id=paper_id)
            gathered: list[tuple[str, SearchHit]] = []
            for provider_id, capability in providers:
                search = getattr(capability, "search_known_item", None)
                if search is None:
                    continue
                try:
                    hits = list(await _invoke_provider(search, parsed, limit=5))
                except Exception as exc:
                    attempts.append(self._attempt(reference_id, provider_id, "known_item", parsed.title or raw, "failed", error=f"{type(exc).__name__}: {exc}"[:1000]))
                    continue
                gathered.extend((provider_id, hit) for hit in hits)
                attempts.append(self._attempt(reference_id, provider_id, "known_item", parsed.title or raw, "completed", hits))
            match = self.matcher.match(parsed, [hit for _, hit in gathered])
            if match.status == ResolutionStatus.RESOLVED and match.selected:
                selected_provider = next(pid for pid, hit in gathered if _hit_key(hit) == _hit_key(match.selected))
                return self._materialize(reference_id, ordinal, raw, parsed, attempts, selected_provider, match.selected, paper_id=paper_id)
            if not attempts:
                attempts.append(self._attempt(reference_id, "bootstrap", "unavailable", raw, "not_found"))
            status = ResolutionStatus.FAILED if attempts and all(item.status == "failed" for item in attempts) else match.status
            return ReferenceBootstrapEntry(reference_id=reference_id, ordinal=ordinal, raw_reference=raw, parsed=parsed, resolution_status=status, attempts=attempts)
        except Exception as exc:
            attempts.append(self._attempt(reference_id, "bootstrap", "resolution", raw, "failed", error=f"{type(exc).__name__}: {exc}"[:1000]))
            return ReferenceBootstrapEntry(reference_id=reference_id, ordinal=ordinal, raw_reference=raw, parsed=parsed, resolution_status=ResolutionStatus.FAILED, attempts=attempts)

    def _materialize(self, reference_id: str, ordinal: int, raw: str, parsed: ParsedCitation, attempts: list[ReferenceResolveAttempt], provider_id: str, hit: SearchHit | None, *, paper_id: str) -> ReferenceBootstrapEntry:
        assert hit is not None
        if not paper_id:
            raise ValueError("paper_id is required for materialization")
        work, record, _ = self.adapter.adapt_hit(hit, provider_id, datetime.now(timezone.utc))
        artifacts: list[Artifact] = []
        if hit.abstract.strip():
            artifact_id = self.adapter.stable_id("art", record.source_record_id, "abstract")
            self.store.write_document(paper_id, work_id=work.work_id, artifact_id=artifact_id, extension="txt", content=hit.abstract)
            artifacts.append(Artifact(artifact_id=artifact_id, work_id=work.work_id, source_record_id=record.source_record_id, role=ArtifactRole.ABSTRACT, media_type="text/plain", relative_path=f"documents/{work.work_id}/{artifact_id}.txt", sha256=hashlib.sha256(hit.abstract.encode()).hexdigest(), byte_size=len(hit.abstract.encode()), content_extent=ContentExtent.FULL, acquired_at=datetime.now(timezone.utc), provenance={"source": "search_hit.abstract"}))
        # 在锁内重新读取再合并：bootstrap 并发解析多条引文，各自持有旧副本整份
        # 写回会互相覆盖（条目静默消失，只剩悬空制品）。
        self.store.merge_manifest(paper_id, works=[work], source_records=[record], artifacts=artifacts)
        attempts[-1] = attempts[-1].model_copy(update={"selected_work_id": work.work_id})
        return ReferenceBootstrapEntry(reference_id=reference_id, ordinal=ordinal, raw_reference=raw, parsed=parsed, resolution_status=ResolutionStatus.RESOLVED, resolved_work_id=work.work_id, attempts=attempts)

    @staticmethod
    def _attempt(reference_id: str, provider_id: str, method: str, query: str, status: str, hits: list[SearchHit] | None = None, error: str | None = None) -> ReferenceResolveAttempt:
        return ReferenceResolveAttempt(attempt_id=StructuredRetrievalAdapter.stable_id("att", reference_id, provider_id, method, str(len(hits or []))), provider_id=provider_id, method=method, query_or_identifier=query, status=status, candidate_work_ids=[StructuredRetrievalAdapter.stable_id("candidate", _hit_key(h)) for h in hits or []], error=error)


def _text(value: str) -> str:
    return re.sub(r"[^\w]+", "", unicodedata.normalize("NFKC", value).casefold())


def references_digest(references: Iterable[str]) -> str:
    """Return an order-sensitive identity for the PaperInput reference list."""

    normalized = [" ".join(item.split()) for item in references]
    payload = "\n".join(normalized).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _doi(value: str) -> str:
    return value.casefold().removeprefix("https://doi.org/").removeprefix("doi:").strip()


def _arxiv(value: str) -> str:
    return value.casefold().removeprefix("arxiv:").split("v", 1)[0]


def _hit_key(hit: SearchHit) -> str:
    return _doi(hit.doi) if hit.doi else f"{hit.source_id or ''}:{hit.external_id or hit.document_id}:{_text(hit.title)}"
