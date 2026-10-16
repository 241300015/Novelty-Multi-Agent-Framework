"""Deterministic workflow-owned compiler from semantic drafts to evidence."""

from __future__ import annotations

import hashlib
import re
from difflib import SequenceMatcher

from ..schemas.failures import FailureCode as C, CodedFailure
from collections.abc import Sequence
from dataclasses import dataclass

from ..persistence import ReferenceStore, reference_store_for_artifact_namespace
from ..schemas import (
    Artifact,
    ArtifactNamespace,
    Evidence,
    EvidenceCard,
    EvidenceCardBuilderResult,
    EvidenceCardDraft,
    EvidenceQuoteDraft,
    EvidenceLocator,
    EvidenceSource,
    ReferenceReadResult,
    ResearchFinishDraft,
    SourceRecord,
    TaskResearchRequest,
    Work,
)


@dataclass(frozen=True)
class _ManifestIndex:
    works: dict[str, Work]
    artifacts: dict[str, Artifact]
    records: dict[str, SourceRecord]


class EvidenceCardBuilder:
    """Bind model-authored semantics to trusted persisted provenance."""

    def __init__(self, reference_store: ReferenceStore) -> None:
        self.reference_store = reference_store

    def build(
        self,
        draft: ResearchFinishDraft,
        *,
        scope: TaskResearchRequest,
        read_results: Sequence[ReferenceReadResult],
    ) -> EvidenceCardBuilderResult:
        draft = ResearchFinishDraft.model_validate(draft)
        scope = TaskResearchRequest.model_validate(scope)
        reads = [ReferenceReadResult.model_validate(item) for item in read_results]
        if not draft.cards:
            return EvidenceCardBuilderResult(
                warnings=[f"no evidence: {draft.no_evidence_reason}"]
            )

        indexes = self._manifest_indexes(scope.subject_paper_id, reads)
        all_evidence: list[Evidence] = []
        cards: list[EvidenceCard] = []
        warnings: list[str] = []
        rejections = []
        resolved_works: set[tuple[ArtifactNamespace, str]] = set()

        prepared = []
        for position, card_draft in enumerate(draft.cards, start=1):
            try:
                matches = [self._matching_reads(q, reads, indexes, scope) for q in card_draft.quotes]
                addresses = [{(r.namespace, r.work_id) for r, _ in group} for group in matches]
                # Preserve the existing whole-card disambiguation rule. Split only
                # when EACH quote has exactly one established source work.
                if not set.intersection(*addresses) and all(len(group) == 1 for group in addresses):
                    grouped = {}
                    for quote, group in zip(card_draft.quotes, addresses, strict=True):
                        grouped.setdefault(next(iter(group)), []).append(quote)
                    audit = {"original_card_index": position - 1,
                             "source_works": [{"namespace": ns.value, "work_id": wid}
                                              for ns, wid in grouped],
                             "semantic_policy": "quote_interpretations_only"}
                    for quotes in grouped.values():
                        # Mixed-card assessments have no per-source attribution.
                        # Preserve quote-local interpretations; do not copy a joint
                        # overlap/baseline claim onto every resulting paper.
                        separated = card_draft.model_copy(update={
                            "quotes": quotes,
                            "main_contribution": "\n".join(dict.fromkeys(q.interpretation for q in quotes)),
                            "overlaps": [], "differences": [], "possible_baseline": False,
                            "confidence": min(card_draft.confidence, *(q.confidence for q in quotes)),
                        })
                        prepared.append((position, separated, audit))
                    warnings.append(f"split evidence card #{position} into {len(grouped)} source works; "
                                    "unattributed overlaps/differences/baseline cleared")
                    continue
            except ValueError:
                pass  # The normal compiler below records the original rejection.
            prepared.append((position, card_draft, None))

        for position, card_draft, split_audit in prepared:
            try:
                matched_by_quote = [
                    self._matching_reads(quote, reads, indexes, scope)
                    for quote in card_draft.quotes
                ]
                candidate_sets = [
                    {(read.namespace, read.work_id) for read, _artifact in matches}
                    for matches in matched_by_quote
                ]
                resolved = set.intersection(*candidate_sets)
                if not resolved:
                    raise CodedFailure(C.BUILDER_CROSS_WORK, "cross-work or inconsistent quote provenance")
                if len(resolved) > 1:
                    raise CodedFailure(C.BUILDER_AMBIGUOUS, "ambiguous quote provenance")
                namespace, work_id = next(iter(resolved))
                work_address = (namespace, work_id)
                if work_address in resolved_works:
                    raise CodedFailure(C.BUILDER_DUPLICATE, f"duplicate evidence card for work {work_id}")
                index = indexes[namespace]
                work = index.works.get(work_id)
                if work is None:
                    raise CodedFailure(C.BUILDER_WORK_MISSING, f"missing Work {work_id}")

                evidence, sources = self._build_card_items(
                    card_draft,
                    matched_by_quote,
                    work,
                    namespace,
                    index,
                    scope,
                    warnings,
                )
                if split_audit is not None:
                    evidence = [item.model_copy(update={"provenance": {
                        **item.provenance, "builder_split": split_audit}}) for item in evidence]
                card = EvidenceCard(
                    card_id=_stable_id(
                        "card",
                        scope.subject_paper_id,
                        scope.novelty_point.point_id,
                        scope.research_task.task_id,
                        namespace.value,
                        work_id,
                    ),
                    task_id=scope.research_task.task_id,
                    novelty_point_id=scope.novelty_point.point_id,
                    document_title=work.title,
                    main_contribution=card_draft.main_contribution,
                    overlaps=card_draft.overlaps,
                    differences=card_draft.differences,
                    sources=sources,
                    cited_by_paper=None,
                    possible_baseline=card_draft.possible_baseline,
                    relevance=card_draft.relevance,
                    confidence=card_draft.confidence,
                    evidence_ids=[item.evidence_id for item in evidence],
                )
            except ValueError as exc:
                # 单张卡的溯源问题只丢弃这一张，同任务其它卡照常产出。
                # 此前这里直接向上抛，导致一条引文失配就作废整轮检索（实测
                # 整轮 0 卡）；受影响的工作不会被登记，后续卡仍可引用它。
                warnings.append(
                    f"dropped evidence card #{position}: {type(exc).__name__}: {exc}"
                )
                rejections.append({"card_index": position - 1, "reason": str(exc),
                                   "code": getattr(exc, "code", C.MODEL_OUTPUT_REFERENCE)})
                continue
            resolved_works.add(work_address)
            all_evidence.extend(evidence)
            cards.append(card)

        return EvidenceCardBuilderResult(
            evidence=all_evidence,
            evidence_cards=cards,
            warnings=list(dict.fromkeys(warnings)),
            rejections=rejections,
        )

    def _manifest_indexes(
        self,
        paper_id: str,
        reads: Sequence[ReferenceReadResult],
    ) -> dict[ArtifactNamespace, _ManifestIndex]:
        indexes: dict[ArtifactNamespace, _ManifestIndex] = {}
        for namespace in dict.fromkeys(read.namespace for read in reads):
            store = reference_store_for_artifact_namespace(
                namespace, output_root=self.reference_store.output_root
            )
            manifest = store.load_manifest(paper_id)
            indexes[namespace] = _ManifestIndex(
                works={item.work_id: item for item in manifest.works},
                artifacts={item.artifact_id: item for item in manifest.artifacts},
                records={
                    item.source_record_id: item for item in manifest.source_records
                },
            )
        return indexes

    @staticmethod
    def _matching_reads(
        quote: EvidenceQuoteDraft,
        reads: Sequence[ReferenceReadResult],
        indexes: dict[ArtifactNamespace, _ManifestIndex],
        scope: TaskResearchRequest | None = None,
    ) -> list[tuple[ReferenceReadResult, Artifact]]:
        matches: list[tuple[ReferenceReadResult, Artifact]] = []
        supplementary_match = False
        generated_match = False
        for read in reads:
            if not _quote_matches(quote.quote, read.text):
                continue
            index = indexes[read.namespace]
            artifact = index.artifacts.get(read.artifact_id)
            if artifact is None:
                raise CodedFailure(C.BUILDER_ARTIFACT_MISSING, f"missing Artifact {read.artifact_id}")
            if artifact.provenance.get("content_origin") == "llm_summary":
                generated_match = True
                continue  # A generated summary cannot become original Evidence via Reader.
            work = index.works.get(read.work_id)
            record = _resolve_source_record(artifact, work, index.records) if work else None
            if _evidence_source_labels(artifact, record)["source_kind"] == "web_supplement":
                supplementary_match = True
                continue  # Web material is advice-only, never a paper Evidence/Card.
            if artifact.work_id != read.work_id:
                raise CodedFailure(C.BUILDER_WORK_MISMATCH,
                    f"Artifact.work_id mismatch for {read.artifact_id}"
                )
            if (
                artifact.source_record_id is not None
                and artifact.source_record_id not in index.records
            ):
                raise CodedFailure(C.BUILDER_RECORD_MISSING,
                    f"Artifact {artifact.artifact_id} references missing SourceRecord"
                )
            matches.append((read, artifact))
        if not matches and supplementary_match:
            raise CodedFailure(C.BUILDER_WEB_SUPPLEMENT, "web supplementary material cannot be used as evidence")
        if not matches:
            code = C.BUILDER_QUOTE_UNGROUNDED
            if generated_match:
                code = C.BUILDER_QUOTE_GENERATED
            elif scope is not None and any(_quote_matches(quote.quote, text)
                    for text in _input_strings(scope.novelty_point.model_dump(include={
                        "claim", "claim_en", "technical_features", "technical_features_en"}))):
                code = C.BUILDER_QUOTE_TARGET
            elif any(_near_quote(quote.quote, read.text) for read in reads):
                code = C.BUILDER_QUOTE_TEXT_MISMATCH
            raise CodedFailure(code, f"ungrounded quote: {quote.quote!r}")
        return matches

    @staticmethod
    def _build_card_items(
        draft: EvidenceCardDraft,
        matched_by_quote: Sequence[list[tuple[ReferenceReadResult, Artifact]]],
        work: Work,
        namespace: ArtifactNamespace,
        index: _ManifestIndex,
        scope: TaskResearchRequest,
        warnings: list[str],
    ) -> tuple[list[Evidence], list[EvidenceSource]]:
        evidence: list[Evidence] = []
        sources: list[EvidenceSource] = []
        for quote, matches in zip(draft.quotes, matched_by_quote, strict=True):
            read, artifact = min(
                (
                    item
                    for item in matches
                    if item[0].namespace == namespace
                    and item[0].work_id == work.work_id
                ),
                key=lambda item: (
                    item[0].namespace.value,
                    item[0].artifact_id,
                    item[0].char_start,
                    item[0].char_end,
                    item[0].read_id,
                ),
            )
            # Re-resolve rather than trusting the tuple retained during matching.
            persisted_artifact = index.artifacts.get(read.artifact_id)
            if persisted_artifact is None:
                raise CodedFailure(C.BUILDER_ARTIFACT_MISSING, f"missing Artifact {read.artifact_id}")
            if persisted_artifact.work_id != work.work_id:
                raise CodedFailure(C.BUILDER_WORK_MISMATCH,
                    f"Artifact.work_id mismatch for {read.artifact_id}"
                )
            record = _resolve_source_record(persisted_artifact, work, index.records)
            if record is None:
                warnings.append(f"missing source record for work {work.work_id}")
            local_start, local_end = _find_quote_span(quote.quote, read.text)
            quote_char_start = read.char_start + local_start
            quote_char_end = read.char_start + local_end
            normalized_quote = _normalize_whitespace(quote.quote)
            item = Evidence(
                evidence_id=_stable_id(
                    "ev",
                    scope.subject_paper_id,
                    scope.novelty_point.point_id,
                    scope.research_task.task_id,
                    namespace.value,
                    work.work_id,
                    persisted_artifact.artifact_id,
                    normalized_quote,
                ),
                work_id=work.work_id,
                artifact_id=persisted_artifact.artifact_id,
                novelty_point_id=scope.novelty_point.point_id,
                task_id=scope.research_task.task_id,
                quote=quote.quote,
                locator=EvidenceLocator(
                    char_start=quote_char_start, char_end=quote_char_end
                ),
                interpretation=quote.interpretation,
                confidence=quote.confidence,
                provenance={
                    **_evidence_source_labels(persisted_artifact, record),
                    "builder": "evidence_card_builder",
                    "artifact_namespace": namespace.value,
                    "read_id": read.read_id,
                    "read_char_start": read.char_start,
                    "read_char_end": read.char_end,
                    "quote_char_start": quote_char_start,
                    "quote_char_end": quote_char_end,
                    **(
                        {"source_record_id": record.source_record_id}
                        if record is not None
                        else {}
                    ),
                },
            )
            evidence.append(item)
            sources.append(
                EvidenceSource(
                    title=work.title,
                    quote=quote.quote,
                    location=(
                        f"artifact {persisted_artifact.artifact_id} "
                        f"chars:{quote_char_start}-{quote_char_end}"
                    ),
                    doi=_doi(work, record),
                    url=(
                        record.full_text_url or record.landing_url
                        if record is not None
                        else None
                    ),
                )
            )
        return evidence, sources


def _evidence_source_labels(artifact: Artifact, record: SourceRecord | None) -> dict[str, str]:
    # Acquisition lineage takes precedence over a work's bibliographic identity.
    kind = record.source_kind.value if record is not None else "unknown"
    if (artifact.provenance.get("source_kind") == "web_supplement"
            or artifact.provenance.get("tool") == "browser"
            or kind in {"web", "web_supplement"}):
        return {"source_kind": "web_supplement", "evidence_type": "web_supplement_evidence"}
    if kind == "structured_database":
        return {"source_kind": kind, "evidence_type": "database_evidence"}
    return {"source_kind": kind, "evidence_type": "unknown"}


def _quote_matches(quote: str, text: str) -> bool:
    return quote in text or _normalize_whitespace(quote) in _normalize_whitespace(text)


# 模型转述正文时会把排版字符「规范化」：弯引号写成直引号、en/em dash 写成连字符、
# 不换行空格写成普通空格。这些字形差异肉眼不可见，却足以让整条引文无法溯源——
# 实测一条 224 字的引文仅因 ’ 与 ' 之差被判定为 ungrounded。
# 注意：必须是**长度不变**的 1:1 替换，否则 _normalized_spans 的偏移映射会失效。
_TYPOGRAPHIC_FOLD = str.maketrans(
    {
        "\u2018": "'",
        "\u2019": "'",
        "\u201b": "'",
        "\u201c": '"',
        "\u201d": '"',
        "\u201f": '"',
        "\u2010": "-",
        "\u2011": "-",
        "\u2012": "-",
        "\u2013": "-",
        "\u2014": "-",
        "\u2015": "-",
        "\u2212": "-",
        "\u00a0": " ",
        "\u2007": " ",
        "\u202f": " ",
    }
)


def fold_quote_typography(value: str) -> str:
    """把排版变体折叠为 ASCII 等价形式（1:1，长度不变）。"""

    return value.translate(_TYPOGRAPHIC_FOLD)


def normalize_quote_whitespace(value: str) -> str:
    """引文比较的统一归一化，builder 与完整性门共用。

    先折叠排版变体，再折叠空白——两者都是模型复现正文时的常见偏差。
    此前只处理空白，于是仅一个弯引号之差就会让引文溯源失败。
    """

    return re.sub(r"\s+", " ", fold_quote_typography(value)).strip()


_normalize_whitespace = normalize_quote_whitespace


_WS_RE = re.compile(r"\s")


def _normalized_spans(value: str) -> tuple[str, list[tuple[int, int]]]:
    """Collapse whitespace like _normalize_whitespace while keeping original offsets."""

    chars: list[str] = []
    spans: list[tuple[int, int]] = []
    index = 0
    while index < len(value):
        if _WS_RE.match(value[index]):
            end = index + 1
            while end < len(value) and _WS_RE.match(value[end]):
                end += 1
            chars.append(" ")
            spans.append((index, end))
            index = end
        else:
            chars.append(fold_quote_typography(value[index]))
            spans.append((index, index + 1))
            index += 1
    return "".join(chars), spans


def _find_quote_span(quote: str, text: str) -> tuple[int, int]:
    """Return the original character span of *quote* inside *text*.

    Exact substring matches are preferred. Whitespace-normalized matches are
    mapped back to original offsets so Evidence.locator always refers to the
    persisted Reader text, never to a normalized copy.
    """

    exact = text.find(quote)
    if exact >= 0:
        return exact, exact + len(quote)

    normalized_text, spans = _normalized_spans(text)
    wanted = _normalize_whitespace(quote)
    normalized_start = normalized_text.find(wanted)
    if normalized_start < 0:
        raise ValueError(f"quote not found in read text: {quote!r}")
    normalized_end = normalized_start + len(wanted)
    if normalized_end > len(spans):
        raise ValueError(f"quote span out of bounds: {quote!r}")
    return spans[normalized_start][0], spans[normalized_end - 1][1]


def _resolve_source_record(
    artifact: Artifact,
    work: Work,
    records: dict[str, SourceRecord],
) -> SourceRecord | None:
    if artifact.source_record_id is not None:
        return records.get(artifact.source_record_id)
    if work.canonical_source_record_id is not None:
        canonical = records.get(work.canonical_source_record_id)
        if canonical is not None:
            return canonical
    candidates = sorted(
        (item for item in records.values() if item.work_id == work.work_id),
        key=lambda item: item.source_record_id,
    )
    return candidates[0] if candidates else None


def _doi(work: Work, record: SourceRecord | None) -> str | None:
    work_doi = next(
        (item.value for item in work.identifiers if item.namespace == "doi"), None
    )
    if work_doi is not None:
        return work_doi
    if record is None:
        return None
    return next(
        (item.value for item in record.identifiers if item.namespace == "doi"), None
    )


def _stable_id(prefix: str, *parts: str) -> str:
    digest = hashlib.sha256("\0".join(parts).encode("utf-8")).hexdigest()
    return f"{prefix}_{digest[:24]}"


def _input_strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for child in value.values():
            yield from _input_strings(child)
    elif isinstance(value, list):
        for child in value:
            yield from _input_strings(child)


def _near_quote(quote: str, text: str) -> bool:
    """Diagnostic only: >= 40 characters, <= 3 changed characters in a local span.

    Never used to accept a quote, assign a source or modify text. Short quotes and
    large rewrites remain unmatched rather than being guessed to be typos.
    """
    if len(quote) < 40:
        return False
    matcher = SequenceMatcher(None, quote, text, autojunk=False)
    anchor = matcher.find_longest_match()
    if anchor.size < len(quote) // 2:
        return False
    start = anchor.b - anchor.a
    for offset in range(-3, 4):
        for length_delta in range(-3, 4):
            left = start + offset
            if left < 0:
                continue
            candidate = text[left:left + len(quote) + length_delta]
            changes = sum(max(i2-i1, j2-j1) for tag,i1,i2,j1,j2 in
                SequenceMatcher(None, quote, candidate, autojunk=False).get_opcodes() if tag != "equal")
            if 0 < changes <= 3:
                return True
    return False
