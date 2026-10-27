"""Shared local checks for Harness-registered Reviewer reads, never model decisions."""
from __future__ import annotations

from ..persistence import reference_store_for_artifact_namespace
from ..schemas import ArtifactNamespace
from ..schemas.failures import FailureCode as C, CodedFailure
from .reviewer_evidence import review_evidence_identity


def validate_registered_review_reads(review, card, evidence, *, paper_id, reference_store):
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
        expected, expected_review = review_evidence_identity(
            card.card_id, item.read_id, item.char_start, item.char_end)
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
