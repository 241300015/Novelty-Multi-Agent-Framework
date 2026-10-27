"""Harness-owned evidence registration and Reviewer model projections."""
from __future__ import annotations

import hashlib

from ..schemas.domain import ReviewEvidence


_INTERNAL_FIELDS = frozenset({
    "read_id", "read_citations", "read_receipts", "reader_observations",
    "read_char_start", "read_char_end", "char_start", "char_end", "locator",
    "provenance", "artifact_hash", "content_hash",
})


def reviewer_model_view(value):
    """Keep runtime coordinates in persisted records, never in model input."""
    if isinstance(value, dict):
        return {key: reviewer_model_view(item) for key, item in value.items()
                if key not in _INTERNAL_FIELDS}
    if isinstance(value, list):
        return [reviewer_model_view(item) for item in value]
    return value


class ReviewerEvidenceRegistry:
    """Invocation-local registry shared by controlled and native Reviewer paths."""

    def __init__(self, request, catalog):
        self.request, self.catalog = request, catalog
        self.evidence = {}

    def choices(self):
        """Only supplied Card evidence and successfully registered reads are selectable."""
        bound = {eid for card in self.request.cards for eid in card.evidence_ids}
        choices = {item.evidence_id: {"work_id": item.work_id, "origin": "card"}
                   for item in self.request.evidence if item.evidence_id in bound}
        choices.update({item.evidence_id: {"work_id": item.work_id, "origin": "reading"}
                        for item in self.evidence.values()})
        return choices

    def project_model_context(self, observation, context):
        if observation.tool_name != "reader":
            return reviewer_model_view(context)
        evidence = []
        if observation.succeeded:
            for raw in observation.payload.get("read_results") or [observation.payload.get("read_result", {})]:
                if raw.get("char_end", 0) <= raw.get("char_start", 0):
                    continue
                item = register_read_evidence(raw, self.request, self.catalog)
                self.evidence[item.evidence_id] = item
                evidence.append({"evidence_id": item.evidence_id, "work_id": item.work_id,
                    "quote": item.exact_quote, "role": item.role,
                    "content_extent": item.content_extent, "has_more": raw.get("has_more", False)})
        return {"succeeded": observation.succeeded, "evidence": evidence,
                "read_status": observation.payload.get("read_status"),
                "error": observation.error,
                "execution_issues": reviewer_model_view(observation.payload.get("execution_issues", []))}


def review_evidence_identity(card_id, read_id, start, end):
    """Stable program-owned identifiers shared by registration and local audits."""
    digest = hashlib.sha256(f"{card_id}\x1f{read_id}\x1f{start}\x1f{end}".encode()).hexdigest()
    return "rev_ev_" + digest[:24], "review_" + hashlib.sha256(card_id.encode()).hexdigest()[:20]


def register_read_evidence(raw, request, catalog):
    """Register the returned slice without asking the model to repeat its locator."""
    start, end = raw["char_start"], raw["char_end"]
    if not raw.get("read_id") or start < 0 or end <= start or len(raw["text"]) != end - start:
        raise ValueError("invalid Reviewer read content or range")
    entry = next((item for item in catalog if item["artifact_id"] == raw["artifact_id"]
                  and item["namespace"] == raw["namespace"]
                  and item["work_id"] == raw["work_id"]), None)
    if entry is None or (entry["content_hash"] is not None and entry["content_hash"] != raw["sha256"]):
        raise ValueError("read is outside the authorized material catalog")
    cards = [card for card in request.cards if any(
        item.work_id == raw["work_id"] and item.evidence_id in card.evidence_ids
        for item in request.evidence)]
    if len(cards) != 1:
        raise ValueError("read must bind one Card and Work")
    card = cards[0]
    evidence_id, review_id = review_evidence_identity(card.card_id, raw["read_id"], start, end)
    return ReviewEvidence(
        evidence_id=evidence_id, review_id=review_id,
        origin_card_id=card.card_id, novelty_point_id=request.novelty_point.point_id,
        work_id=raw["work_id"], source_record_id=entry["source_record_id"],
        artifact_id=raw["artifact_id"], namespace=raw["namespace"],
        artifact_hash=raw["sha256"], read_id=raw["read_id"], char_start=start, char_end=end,
        exact_quote=raw["text"], role=entry["role"], content_extent=entry["content_extent"],
        version_label=entry["version_label"],
    )
