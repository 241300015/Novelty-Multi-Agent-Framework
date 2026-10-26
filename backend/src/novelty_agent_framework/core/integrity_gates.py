"""Deterministic referential-integrity gates for synthesis inputs and reports."""

from __future__ import annotations

from ..schemas.failures import FailureCode as C, CodedFailure
from .failure_classification import output_failure_code
from pydantic import ValidationError

from collections import Counter
import re
from dataclasses import dataclass
from typing import Any, Sequence
from urllib.parse import urlsplit, urlunsplit

from ..persistence import ReferenceStore, reference_store_for_artifact_namespace
from ..schemas import (
    ArtifactNamespace,
    Evidence,
    EvidenceCard,
    NoveltyPoint,
    NoveltyPointReview,
    NoveltyReport,
    ResearchTask,
)
from ..tools.evidence_card_builder import normalize_quote_whitespace

_DEFAULT_NAMESPACE = ArtifactNamespace.RESEARCH_REFERENCE


@dataclass(frozen=True)
class _ManifestIndex:
    """单个命名空间的 Manifest 索引及其存储。"""

    works: dict[str, Any]
    artifacts: dict[str, Any]
    records: dict[str, Any]
    store: ReferenceStore


def _evidence_namespace(evidence: Evidence) -> ArtifactNamespace:
    """Evidence 的来源命名空间；Builder 会把它写进 provenance。

    历史卡片没有这个字段，回退到研究语料——与改造前的行为一致。
    """

    raw = (evidence.provenance or {}).get("artifact_namespace")
    if not raw:
        return _DEFAULT_NAMESPACE
    try:
        return ArtifactNamespace(raw)
    except ValueError:
        return _DEFAULT_NAMESPACE


def _manifest_indexes(
    paper_id: str,
    namespaces: set[ArtifactNamespace],
    reference_store: ReferenceStore,
) -> dict[ArtifactNamespace, _ManifestIndex]:
    """按命名空间分别加载 Manifest。

    研究语料（``references/``）与论文自带参考语料（``subject_references/``）是两套
    独立存储。``EvidenceCardBuilder`` 已按命名空间分别索引，门控必须保持一致，
    否则来自自带参考语料的合法证据会被误判为「缺失 Work/Artifact」——实测两张
    这样的卡片被 Gate A 拒掉，而它们本来是通过 Validator 的有效证据。
    """

    indexes: dict[ArtifactNamespace, _ManifestIndex] = {}
    for namespace in namespaces:
        store = reference_store_for_artifact_namespace(
            namespace, output_root=reference_store.output_root
        )
        # Loading validates the manifest's own Work/SourceRecord/Artifact relations.
        manifest = store.load_manifest(paper_id)
        indexes[namespace] = _ManifestIndex(
            works={item.work_id: item for item in manifest.works},
            artifacts={item.artifact_id: item for item in manifest.artifacts},
            records={item.source_record_id: item for item in manifest.source_records},
            store=store,
        )
    return indexes


@dataclass(frozen=True)
class SynthesisIntegrityResult:
    accepted: tuple[EvidenceCard, ...]
    rejected: tuple[tuple[EvidenceCard, tuple[str, ...]], ...]

    def audit(self) -> dict[str, Any]:
        return {
            "validation_passed": not self.rejected,
            "checked_card_count": len(self.accepted) + len(self.rejected),
            "accepted_card_count": len(self.accepted),
            "rejected_card_count": len(self.rejected),
            "accepted_card_ids": [card.card_id for card in self.accepted],
            "rejected_card_ids": [card.card_id for card, _ in self.rejected],
            "rejected_cards": [
                {"card_id": card.card_id, "reasons": list(reasons)}
                for card, reasons in self.rejected
            ],
        }


@dataclass(frozen=True)
class ReportIntegrityResult:
    validation_passed: bool
    checked_conclusion_count: int
    referenced_card_count: int
    reference_occurrence_count: int
    review_count: int
    conclusion_count: int
    matched_count: int
    mismatch_count: int
    mismatched_point_ids: tuple[str, ...]
    issues: tuple[str, ...]
    checked_partial_card_count: int = 0

    def audit(self) -> dict[str, Any]:
        return {
            "validation_passed": self.validation_passed,
            "checked_conclusion_count": self.checked_conclusion_count,
            "checked_partial_card_count": self.checked_partial_card_count,
            "referenced_card_count": self.referenced_card_count,
            "reference_occurrence_count": self.reference_occurrence_count,
            "review_count": self.review_count,
            "conclusion_count": self.conclusion_count,
            "matched_count": self.matched_count,
            "mismatch_count": self.mismatch_count,
            "mismatched_point_ids": list(self.mismatched_point_ids),
            "issues": list(self.issues),
        }


def validate_card_integrity(
    cards: Sequence[EvidenceCard],
    *,
    evidence: Sequence[Evidence],
    tasks: Sequence[ResearchTask],
    novelty_points: Sequence[NoveltyPoint],
    paper_id: str,
    reference_store: ReferenceStore,
) -> SynthesisIntegrityResult:
    """Validator's provenance check, before Reviewer sees any Card."""

    # Loading validates the manifest's own Work/SourceRecord/Artifact relations.
    indexes = _manifest_indexes(
        paper_id,
        {_evidence_namespace(item) for item in evidence} | {_DEFAULT_NAMESPACE},
        reference_store,
    )
    point_ids = {item.point_id for item in novelty_points}
    task_ids = {task.task_id for task in tasks}
    task_pairs = {(task.task_id, task.novelty_point_id) for task in tasks}
    evidence_candidates: dict[str, list[Evidence]] = {}
    for item in evidence:
        evidence_candidates.setdefault(item.evidence_id, []).append(item)
    card_id_counts = Counter(card.card_id for card in cards)
    verified_files: dict[tuple[ArtifactNamespace, str], bytes] = {}
    file_errors: dict[tuple[ArtifactNamespace, str], str] = {}

    accepted: list[EvidenceCard] = []
    rejected: list[tuple[EvidenceCard, tuple[str, ...]]] = []
    for card in cards:
        reasons: list[str] = []
        if card_id_counts[card.card_id] != 1:
            reasons.append(f"duplicate card_id: {card.card_id}")
        if card.task_id not in task_ids:
            reasons.append(f"unknown task_id: {card.task_id}")
        elif (card.task_id, card.novelty_point_id) not in task_pairs:
            reasons.append(
                "card task/novelty-point mismatch: "
                f"{card.task_id} does not belong to {card.novelty_point_id}"
            )
        if card.novelty_point_id not in point_ids:
            reasons.append(f"unknown novelty_point_id: {card.novelty_point_id}")
        if not card.evidence_ids:
            reasons.append("evidence_ids is empty")

        resolved_evidence: list[Evidence] = []
        for evidence_id in card.evidence_ids:
            candidates = evidence_candidates.get(evidence_id, [])
            if not candidates:
                reasons.append(f"missing Evidence: {evidence_id}")
                continue
            distinct = {
                item.model_dump_json(exclude_none=False) for item in candidates
            }
            if len(distinct) > 1:
                reasons.append(f"ambiguous Evidence: {evidence_id}")
                continue
            resolved_evidence.append(candidates[0])

        for item in resolved_evidence:
            reasons.extend(
                _validate_evidence_chain(
                    card,
                    item,
                    indexes=indexes,
                    paper_id=paper_id,
                    verified_files=verified_files,
                    file_errors=file_errors,
                )
            )

        reasons.extend(_validate_card_locations(card, resolved_evidence))
        reasons.extend(_validate_card_metadata(card, resolved_evidence, indexes))

        unique_reasons = tuple(dict.fromkeys(reasons))
        if unique_reasons:
            rejected.append((card, unique_reasons))
        else:
            accepted.append(card)

    return SynthesisIntegrityResult(tuple(accepted), tuple(rejected))


# Compatibility for standalone historical probes. Workflow Gate A uses
# validate_review_integrity; new Card consumers use validate_card_integrity.
validate_synthesis_input = validate_card_integrity



@dataclass(frozen=True)
class ReviewIntegrityResult:
    reviews: tuple[NoveltyPointReview, ...]
    checked_review_count: int
    rejected: tuple[dict[str, Any], ...]

    def audit(self) -> dict[str, Any]:
        return {
            "validation_passed": not self.rejected,
            "checked_review_count": self.checked_review_count,
            "output_review_count": len(self.reviews),
            "rejected_review_count": len(self.rejected),
            "rejected_reviews": list(self.rejected),
        }


def _validate_review_reads(review, cards, evidence, *, paper_id, reference_store):
    """Validate point-level reads, then each Card's registered additions."""
    # Check every observation/citation, including unused reads, against the NP's
    # authorized Works. The per-Card pass below checks registration ownership.
    if not cards:
        if review.reader_observations or review.review_evidence or review.read_citations:
            raise CodedFailure(C.GATE_CARDS_MISSING, "Review reads require accepted Cards")
        return
    _validate_partial_review_reads(
        review.model_copy(update={"review_evidence": [], "read_citations": []}),
        cards[0], evidence, paper_id=paper_id, reference_store=reference_store)
    for citation in review.read_citations:
        audit = next((x for x in review.reader_observations if x.read_id == citation.read_id), None)
        if audit is None:
            raise CodedFailure(C.GATE_READ_UNVERIFIED, "read citation has no verified Reader observation")
        start = audit.char_start if citation.char_start is None else citation.char_start
        end = audit.char_end if citation.char_end is None else citation.char_end
        if not any(x.read_id == citation.read_id and x.char_start == start and x.char_end == end
                   for x in review.review_evidence):
            raise CodedFailure(C.GATE_INCREMENT_MISSING, "read citation has no registered incremental Evidence")
    for card in cards:
        additions = [x for x in review.review_evidence if x.origin_card_id == card.card_id]
        if not additions:
            continue
        read_ids = {x.read_id for x in additions}
        subset = review.model_copy(update={"review_evidence": additions, "read_citations": [],
            "reader_observations": [x for x in review.reader_observations if x.read_id in read_ids]})
        _validate_partial_review_reads(subset, card,
            [x for x in evidence if x.evidence_id in card.evidence_ids],
            paper_id=paper_id, reference_store=reference_store)


def validate_review_integrity(
    reviews: Sequence[NoveltyPointReview], *, evidence_cards: Sequence[EvidenceCard],
    evidence: Sequence[Evidence], tasks: Sequence[ResearchTask],
    novelty_points: Sequence[NoveltyPoint], paper_id: str, reference_store: ReferenceStore,
) -> ReviewIntegrityResult:
    """Gate A: audit Reviews without changing Validator's accepted Cards.

    Invalid/missing Reviews become explicit technical failures, never scientific
    verdicts. Keep their original payloads in the audit for diagnosis/recovery.
    """
    from ..schemas import NoveltyPointReviewRequest, ReviewStatus, NoveltyVerdict
    from ..schemas.failures import FailureCode, make_failure
    from .review_validation import validate_review_references, has_complete_work_coverage

    point_ids = [point.point_id for point in novelty_points]
    if len(set(point_ids)) != len(point_ids):
        raise ValueError("duplicate novelty point in Gate A input")
    grouped: dict[str, list[NoveltyPointReview]] = {}
    rejected: list[dict[str, Any]] = []
    for review in reviews:
        grouped.setdefault(review.novelty_point_id, []).append(review)
        if review.novelty_point_id not in point_ids:
            rejected.append({"novelty_point_id": review.novelty_point_id,
                "reasons": ["unknown novelty_point_id"],
                "failure_codes": [C.REVIEW_POINT_SCOPE.value],
                "reviews": [review.model_dump(mode="json")]})
    outputs = []
    for point in novelty_points:
        candidates = grouped.get(point.point_id, [])
        try:
            if len(candidates) != 1:
                raise CodedFailure(C.GATE_REVIEW_MISSING if not candidates else C.GATE_REVIEW_DUPLICATE,
                    "missing Review" if not candidates else "duplicate Reviews for novelty point")
            review = NoveltyPointReview.model_validate(candidates[0].model_dump(mode="json"))
            cards = [x for x in evidence_cards if x.novelty_point_id == point.point_id]
            bound_ids = {eid for card in cards for eid in card.evidence_ids}
            scoped_evidence = []
            for evidence_id in bound_ids:
                candidates_for_id = [
                    x for x in evidence
                    if x.evidence_id == evidence_id
                    and x.novelty_point_id == point.point_id
                ]
                canonical = {
                    item.model_dump_json(): item for item in candidates_for_id
                }
                if len(canonical) != 1:
                    raise CodedFailure(
                        C.REVIEW_EVIDENCE_COLLISION,
                        f"original Evidence ID is missing or ambiguous: {evidence_id}",
                    )
                scoped_evidence.append(next(iter(canonical.values())))
            request = NoveltyPointReviewRequest(subject_paper_id=paper_id, novelty_point=point,
                tasks=[x for x in tasks if x.novelty_point_id == point.point_id],
                cards=cards, evidence=scoped_evidence)
            validate_review_references(review, request)
            _validate_review_reads(review, cards, scoped_evidence, paper_id=paper_id,
                                   reference_store=reference_store)
            if review.status is ReviewStatus.REVIEWED:
                if not cards or not scoped_evidence:
                    raise CodedFailure(C.GATE_CARDS_MISSING, "reviewed result requires accepted Cards and Evidence")
                if review.incomplete_reason is not None:
                    raise CodedFailure(C.GATE_INCOMPLETE_CONFLICT, "reviewed result cannot have incomplete_reason")
                if any(x.conclusion_effect in {"blocks_point", "blocks_run"} for x in review.execution_issues):
                    raise CodedFailure(C.GATE_BLOCKING_FAILURE, "reviewed result retains blocking execution failures")
                if review.verdict is NoveltyVerdict.NOT_NOVEL and not has_complete_work_coverage(review, point):
                    raise CodedFailure(C.GATE_COVERAGE_INCOMPLETE, "not_novel requires one Work supporting every declared feature")
            outputs.append(review)
        except (ValueError, TypeError, OSError) as exc:
            reason = str(exc)
            failure_code = (output_failure_code(exc) if isinstance(exc, ValidationError)
                            else getattr(exc, "code", FailureCode.MODEL_OUTPUT_REFERENCE))
            rejected.append({"novelty_point_id": point.point_id, "reasons": [reason],
                "failure_codes": [failure_code.value],
                "reviews": [x.model_dump(mode="json") for x in candidates]})
            event = make_failure(failure_code,
                scope={"node": "gate_a.review", "paper_id": paper_id, "point_id": point.point_id},
                message="Gate A Review integrity: " + reason)
            outputs.append(NoveltyPointReview(novelty_point_id=point.point_id,
                status=ReviewStatus.INSUFFICIENT_EVIDENCE, incomplete_reason="technical_error",
                execution_issues=[event]))
    return ReviewIntegrityResult(tuple(outputs), len(reviews), tuple(rejected))


def _validate_card_locations(card, evidence):
    """Require both location representations to bind the same quote and range."""
    reasons = []
    matched_ids = set()
    if not card.sources:
        reasons.append("Card sources are empty: no location binding")
    for position, source in enumerate(card.sources):
        prefix = f"Card source #{position + 1}"
        if not source.location or not source.location.strip():
            reasons.append(f"{prefix} location is missing")
            continue
        location = re.fullmatch(r"artifact\s+(\S+)\s+chars:([0-9]+)-([0-9]+)", source.location.strip())
        if location is None:
            reasons.append(f"{prefix} location format is invalid")
            continue
        artifact_id, start, end = location.groups()
        matches = [item for item in evidence
                   if item.locator is not None
                   and item.artifact_id == artifact_id
                   and (item.locator.char_start, item.locator.char_end) == (int(start), int(end))
                   and source.quote and normalize_quote_whitespace(source.quote)
                       == normalize_quote_whitespace(item.quote)]
        if not matches:
            reasons.append(f"{prefix} location/quote does not match bound Evidence")
        else:
            matched_ids.update(item.evidence_id for item in matches)
    for item in evidence:
        if item.evidence_id not in matched_ids:
            reasons.append(f"Evidence has no matching Card source location: {item.evidence_id}")
    return reasons


def _metadata_url(value):
    """Normalize HTTP identity only; do not follow redirects or discard query/path."""
    try:
        parts = urlsplit(value.strip())
        if parts.scheme.lower() not in {"http", "https"} or not parts.hostname:
            return None
        if parts.username is not None or parts.password is not None:
            return None
        port = parts.port
        host = parts.hostname.lower()
        if ":" in host:
            host = f"[{host}]"
        if port is not None and (parts.scheme.lower(), port) not in {("http", 80), ("https", 443)}:
            host += f":{port}"
        return urlunsplit((parts.scheme.lower(), host, parts.path or "/", parts.query, parts.fragment))
    except ValueError:
        return None


def _validate_card_metadata(card, evidence, indexes):
    """Compare displayed fields with the local Work bound to each source quote."""
    def title(value):
        return " ".join(value.split()).casefold()

    def doi(value):
        value = value.strip().casefold()
        for prefix in ("https://doi.org/", "http://doi.org/", "doi:"):
            if value.startswith(prefix):
                value = value[len(prefix):]
        return value.strip()

    groups = {}
    reasons = []
    for item in evidence:
        namespace = _evidence_namespace(item)
        index = indexes[namespace]
        group = groups.setdefault((namespace, item.work_id),
                                  {"titles": set(), "dois": set(), "urls": set(), "quotes": set()})
        work = index.works.get(item.work_id)
        records = [record for record in index.records.values() if record.work_id == item.work_id]
        for record in ([work] if work else []) + records:
            group["titles"].add(title(record.title))
            group["dois"].update(doi(identifier.value) for identifier in record.identifiers
                                 if identifier.namespace == "doi")
            raw_urls = [identifier.value for identifier in record.identifiers
                        if identifier.namespace == "url"]
            raw_urls += [getattr(record, field, None) for field in ("landing_url", "full_text_url")]
            group["urls"].update(normalized for value in raw_urls
                                 if value and (normalized := _metadata_url(value)))
        record_id = (item.provenance or {}).get("source_record_id")
        if record_id and (record_id not in index.records or index.records[record_id].work_id != item.work_id):
            reasons.append(f"Evidence SourceRecord binding mismatch: {item.evidence_id}")
        group["quotes"].add(normalize_quote_whitespace(item.quote))
    if groups and not any(title(card.document_title) in group["titles"] for group in groups.values()):
        reasons.append("Card title differs from bound Work/SourceRecord")
    for source in card.sources:
        candidates = [group for group in groups.values()
                      if not source.quote or normalize_quote_whitespace(source.quote) in group["quotes"]]
        if not candidates:
            reasons.append("Card source quote is not linked to bound Evidence")
            continue
        candidates = [group for group in candidates if title(source.title) in group["titles"]]
        if not candidates:
            reasons.append("Card source title differs from bound Work/SourceRecord")
            continue
        if source.doi is not None:
            if not any(group["dois"] for group in candidates):
                reasons.append("Card source DOI cannot be verified: bound Work/SourceRecord has no DOI")
            elif not doi(source.doi) or not any(doi(source.doi) in group["dois"] for group in candidates):
                reasons.append("Card source DOI differs from bound Work/SourceRecord")
            candidates = [group for group in candidates if doi(source.doi) in group["dois"]]
        if source.url is not None:
            normalized = _metadata_url(source.url)
            if normalized is None:
                reasons.append("Card source URL is not a valid HTTP(S) URL")
            elif not any(group["urls"] for group in candidates):
                reasons.append("Card source URL cannot be verified: bound Work/SourceRecord has no matching URL metadata")
            elif not any(normalized in group["urls"] for group in candidates):
                reasons.append("Card source URL differs from bound Work/SourceRecord")
    return reasons


def _validate_evidence_chain(
    card: EvidenceCard,
    evidence: Evidence,
    *,
    indexes: dict[ArtifactNamespace, _ManifestIndex],
    paper_id: str,
    verified_files: dict[tuple[ArtifactNamespace, str], bytes],
    file_errors: dict[tuple[ArtifactNamespace, str], str],
) -> list[str]:
    reasons: list[str] = []
    if evidence.task_id != card.task_id:
        reasons.append(
            f"cross-task Evidence: {evidence.evidence_id} belongs to "
            f"{evidence.task_id}, not {card.task_id}"
        )
    if evidence.novelty_point_id != card.novelty_point_id:
        reasons.append(
            f"cross-point Evidence: {evidence.evidence_id} belongs to "
            f"{evidence.novelty_point_id}, not {card.novelty_point_id}"
        )
    namespace = _evidence_namespace(evidence)
    index = indexes.get(namespace)
    if index is None:
        reasons.append(
            f"missing manifest for namespace {namespace.value}: "
            f"{evidence.evidence_id}"
        )
        return reasons
    if evidence.work_id not in index.works:
        reasons.append(f"missing Work: {evidence.work_id}")
    artifact = index.artifacts.get(evidence.artifact_id)
    if artifact is None:
        reasons.append(f"missing Artifact: {evidence.artifact_id}")
        return reasons
    if artifact.work_id != evidence.work_id:
        reasons.append(
            f"Artifact/Evidence work mismatch: {artifact.artifact_id} belongs to "
            f"{artifact.work_id}, not {evidence.work_id}"
        )

    # 缓存键必须带上命名空间：两个语料里可能存在同名的裸 artifact_id。
    key = (namespace, artifact.artifact_id)
    if key not in verified_files and key not in file_errors:
        try:
            _verified, _path, raw = index.store.verify_artifact_file(
                paper_id, artifact.artifact_id
            )
            verified_files[key] = raw
        except (OSError, ValueError) as exc:
            file_errors[key] = f"{type(exc).__name__}: {exc}"
    file_error = file_errors.get(key)
    if file_error is not None:
        reasons.append(f"invalid Artifact file {artifact.artifact_id}: {file_error}")
        return reasons

    locator = evidence.locator
    if locator is None:
        reasons.append(f"missing Evidence locator: {evidence.evidence_id}")
        return reasons
    start, end = locator.char_start, locator.char_end
    if start is None or end is None:
        reasons.append(f"incomplete character locator: {evidence.evidence_id}")
        return reasons
    raw = verified_files[key]
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        reasons.append(f"Artifact is not UTF-8 text: {artifact.artifact_id}")
        return reasons
    if start < 0 or start >= end or end > len(text):
        reasons.append(
            f"invalid character locator: {evidence.evidence_id} "
            f"[{start}, {end}) for length {len(text)}"
        )
        return reasons
    located = text[start:end]
    if normalize_quote_whitespace(located) != normalize_quote_whitespace(
        evidence.quote
    ):
        reasons.append(f"quote/locator mismatch: {evidence.evidence_id}")
    return reasons


def _validate_partial_review_reads(review, card, evidence, *, paper_id, reference_store):
    """Recheck local Reader provenance; do not infer semantic support from quotes."""
    import hashlib

    authorized_works = {item.work_id for item in evidence}
    observations = {item.read_id: item for item in review.reader_observations}
    if len(observations) != len(review.reader_observations):
        raise ValueError("duplicate Reader observation")
    reads, artifacts = {}, {}
    for audit in observations.values():
        if audit.work_id not in authorized_works or audit.char_start > audit.char_end:
            raise ValueError("Reader observation is outside Card Work/range scope")
        namespace = ArtifactNamespace(audit.namespace)
        store = reference_store_for_artifact_namespace(
            namespace, output_root=reference_store.output_root)
        artifact, _, _ = store.verify_artifact_file(paper_id, audit.artifact_id)
        read = store.read_document_slice(paper_id, artifact_id=audit.artifact_id,
            char_start=audit.char_start, max_chars=max(1, audit.char_end-audit.char_start))
        if (artifact.work_id != audit.work_id or read.work_id != audit.work_id
                or read.namespace.value != audit.namespace or read.read_id != audit.read_id
                or read.char_start != audit.char_start or read.char_end != audit.char_end
                or read.sha256 != audit.artifact_hash
                or hashlib.sha256(read.text.encode()).hexdigest() != audit.text_sha256):
            raise ValueError("Reader observation differs from verified Artifact slice")
        reads[audit.read_id], artifacts[audit.read_id] = read, artifact
    for item in review.review_evidence:
        read = reads.get(item.read_id)
        if read is None:
            raise ValueError("incremental Evidence lacks a verified Reader observation")
        artifact = artifacts[item.read_id]
        expected = "rev_ev_" + hashlib.sha256(
            f"{card.card_id}\x1f{item.read_id}\x1f{item.char_start}\x1f{item.char_end}".encode()).hexdigest()[:24]
        expected_review = "review_" + hashlib.sha256(card.card_id.encode()).hexdigest()[:20]
        if (item.evidence_id != expected or item.review_id != expected_review
                or item.origin_card_id != card.card_id
                or item.novelty_point_id != card.novelty_point_id
                or item.artifact_id != read.artifact_id or item.namespace != read.namespace.value
                or item.work_id != read.work_id or item.artifact_hash != read.sha256
                or item.source_record_id != artifact.source_record_id
                or item.role != artifact.role.value or item.content_extent != artifact.content_extent.value
                or item.version_label != artifact.version_label
                or not read.char_start <= item.char_start < item.char_end <= read.char_end
                or read.text[item.char_start-read.char_start:item.char_end-read.char_start] != item.exact_quote):
            raise ValueError("incremental Evidence registration/source binding mismatch")
    for citation in review.read_citations:
        read = reads.get(citation.read_id)
        if read is None:
            raise CodedFailure(C.GATE_READ_UNVERIFIED, "read citation has no verified Reader observation")
        start = read.char_start if citation.char_start is None else citation.char_start
        end = read.char_end if citation.char_end is None else citation.char_end
        if not any(item.read_id == citation.read_id and item.char_start == start
                   and item.char_end == end for item in review.review_evidence):
            raise CodedFailure(C.GATE_INCREMENT_MISSING, "read citation has no registered incremental Evidence")


def _partial_card_review_issues(report, *, novelty_points, evidence_cards, evidence, reference_store):
    """Audit system-owned partial facts against the final accepted Card set."""
    if not report.partial_card_reviews:
        return []
    # Lazy import keeps the existing Reviewer -> integrity-gate dependency acyclic.
    from .review_validation import validate_review_references
    from ..schemas import NoveltyPointReviewRequest
    from ..schemas.domain import PartialCardReview

    issues = []
    points = {item.point_id: item for item in novelty_points}
    cards = {item.card_id: item for item in evidence_cards}
    card_counts = Counter(item.card_id for item in evidence_cards)
    row_counts = Counter(item.card_id for item in report.partial_card_reviews)
    candidates = {}
    for item in evidence:
        candidates.setdefault(item.evidence_id, []).append(item)
    for raw_row in report.partial_card_reviews:
        prefix = f"partial card review {raw_row.novelty_point_id} -> {raw_row.card_id}"
        try:
            row = PartialCardReview.model_validate(raw_row.model_dump(mode="json"))
            if row_counts[row.card_id] != 1:
                raise ValueError("duplicate partial Card result")
            card, point = cards.get(row.card_id), points.get(row.novelty_point_id)
            if card is None or card_counts[row.card_id] != 1:
                raise ValueError("Card is absent or ambiguous in final accepted set")
            if point is None or card.novelty_point_id != point.point_id:
                raise ValueError("Card/point binding mismatch")
            bound = []
            if not card.evidence_ids:
                raise ValueError("Card has no original Evidence")
            for evidence_id in card.evidence_ids:
                matches = candidates.get(evidence_id, [])
                if not matches or len({item.model_dump_json() for item in matches}) != 1:
                    raise ValueError(f"original Evidence is missing or ambiguous: {evidence_id}")
                bound.append(matches[0])
            request = NoveltyPointReviewRequest(subject_paper_id=report.paper_id,
                novelty_point=point, cards=[card], evidence=bound)
            validate_review_references(row.review, request)
            if reference_store is None:
                raise ValueError("ReferenceStore is required to verify partial source closure")
            indexes = _manifest_indexes(report.paper_id,
                {_evidence_namespace(item) for item in bound}, reference_store)
            verified_files, file_errors = {}, {}
            reasons = [reason for item in bound for reason in _validate_evidence_chain(
                card, item, indexes=indexes, paper_id=report.paper_id,
                verified_files=verified_files, file_errors=file_errors)]
            if reasons:
                raise ValueError("; ".join(reasons))
            _validate_partial_review_reads(row.review, card, bound,
                paper_id=report.paper_id, reference_store=reference_store)
        except (OSError, ValueError) as exc:
            issues.append(f"{prefix}: {exc}")
    return issues


def validate_report_integrity(
    report: NoveltyReport,
    *,
    novelty_points: Sequence[NoveltyPoint],
    evidence_cards: Sequence[EvidenceCard],
    novelty_reviews: Sequence[NoveltyPointReview],
    evidence: Sequence[Evidence] = (),
    reference_store: ReferenceStore | None = None,
) -> ReportIntegrityResult:
    """Check point, card, and authoritative Reviewer closure."""

    expected = Counter(point.point_id for point in novelty_points)
    actual = Counter(item.novelty_point_id for item in report.conclusions)
    issues: list[str] = []
    for point_id, count in expected.items():
        if actual[point_id] < count:
            issues.append(f"missing conclusion: {point_id}")
        elif actual[point_id] > count:
            issues.append(f"duplicate conclusion: {point_id}")
    for point_id in actual.keys() - expected.keys():
        issues.append(f"unknown conclusion: {point_id}")

    review_counts = Counter(review.novelty_point_id for review in novelty_reviews)
    mismatched_point_ids: set[str] = set()
    matched_count = 0
    for point_id, count in expected.items():
        if review_counts[point_id] < count:
            issues.append(f"missing review: {point_id}")
            mismatched_point_ids.add(point_id)
        elif review_counts[point_id] > count:
            issues.append(f"duplicate review: {point_id}")
            mismatched_point_ids.add(point_id)
        if actual[point_id] != count:
            mismatched_point_ids.add(point_id)
    for point_id in review_counts.keys() - expected.keys():
        issues.append(f"unknown review: {point_id}")
        mismatched_point_ids.add(point_id)

    reviews_by_id = {
        review.novelty_point_id: review
        for review in novelty_reviews
        if review_counts[review.novelty_point_id] == 1
    }
    conclusions_by_id = {
        conclusion.novelty_point_id: conclusion
        for conclusion in report.conclusions
        if actual[conclusion.novelty_point_id] == 1
    }
    for point_id in expected:
        review = reviews_by_id.get(point_id)
        conclusion = conclusions_by_id.get(point_id)
        if review is None or conclusion is None:
            continue
        mismatches = []
        if conclusion.review_status != review.status:
            mismatches.append("review_status")
        if conclusion.verdict != review.verdict:
            mismatches.append("verdict")
        if conclusion.verdict_reason != review.verdict_reason:
            mismatches.append("verdict_reason")
        if conclusion.confidence != review.confidence:
            mismatches.append("confidence")
        if conclusion.highly_relevant_works != review.highly_relevant_works:
            mismatches.append("highly_relevant_works")
        if conclusion.review_evidence != review.review_evidence:
            mismatches.append("review_evidence")
        if conclusion.feature_comparisons != review.feature_comparisons:
            mismatches.append("feature_comparisons")
        if conclusion.incomplete_reason != review.incomplete_reason:
            mismatches.append("incomplete_reason")
        if mismatches:
            issues.append(
                f"review/conclusion mismatch: {point_id} fields={','.join(mismatches)}"
            )
            mismatched_point_ids.add(point_id)
        else:
            matched_count += 1

    cards_by_id = {card.card_id: card for card in evidence_cards}
    referenced: list[str] = []
    for conclusion in report.conclusions:
        supporting = conclusion.supporting_card_ids
        counter = conclusion.counter_card_ids
        referenced.extend(supporting)
        referenced.extend(counter)
        for label, card_ids in (("supporting", supporting), ("counter", counter)):
            for card_id, count in Counter(card_ids).items():
                if count > 1:
                    issues.append(
                        f"duplicate {label} card reference: "
                        f"{conclusion.novelty_point_id} -> {card_id}"
                    )
            for card_id in card_ids:
                card = cards_by_id.get(card_id)
                if card is None:
                    issues.append(f"unknown card reference: {card_id}")
                elif card.novelty_point_id != conclusion.novelty_point_id:
                    issues.append(
                        f"cross-point reference: {conclusion.novelty_point_id} -> "
                        f"{card_id} (belongs to {card.novelty_point_id})"
                    )
        for card_id in sorted(set(supporting) & set(counter)):
            issues.append(
                f"supporting/counter conflict: "
                f"{conclusion.novelty_point_id} -> {card_id}"
            )

    issues.extend(_partial_card_review_issues(report, novelty_points=novelty_points,
        evidence_cards=evidence_cards, evidence=evidence, reference_store=reference_store))
    unique_issues = tuple(dict.fromkeys(issues))
    return ReportIntegrityResult(
        validation_passed=not unique_issues,
        checked_conclusion_count=len(report.conclusions),
        checked_partial_card_count=len(report.partial_card_reviews),
        referenced_card_count=len(set(referenced)),
        reference_occurrence_count=len(referenced),
        review_count=len(novelty_reviews),
        conclusion_count=len(report.conclusions),
        matched_count=matched_count,
        mismatch_count=len(mismatched_point_ids),
        mismatched_point_ids=tuple(sorted(mismatched_point_ids)),
        issues=unique_issues,
    )


__all__ = [
    "ReportIntegrityResult",
    "SynthesisIntegrityResult",
    "validate_report_integrity",
    "validate_synthesis_input",
    "validate_card_integrity",
    "validate_review_integrity",
    "ReviewIntegrityResult",
]
