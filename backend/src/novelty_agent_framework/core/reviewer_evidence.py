"""Invocation-local evidence ledger. Registration does not assert semantic support."""
from __future__ import annotations

import hashlib

from ..schemas.domain import ReviewEvidence, ReviewerReadAudit


def reviewer_model_view(value):
    """Expose evidence handles, not runtime-owned read locators or receipts."""
    hidden = {"provenance", "locator", "read_id", "char_start", "char_end",
              "artifact_hash", "content_hash", "reader_observations"}
    if isinstance(value, dict):
        return {key: reviewer_model_view(item) for key, item in value.items() if key not in hidden}
    if isinstance(value, list):
        return [reviewer_model_view(item) for item in value]
    return value


class ReviewerEvidenceLedger:
    def __init__(self, request, catalog):
        self.request, self.catalog = request, catalog
        self.evidence = {}
        self.audits = {}

    def register(self, raw, call):
        """Bind actual output to the dispatched read, authorized Work and source hash."""
        args = call.arguments
        start, end = raw["char_start"], raw["char_end"]
        if (not raw.get("read_id") or raw["artifact_id"] != args["artifact_id"]
                or start != args["char_start"] or not start <= end <= start + args["max_chars"]
                or len(raw["text"]) != end - start):
            raise ValueError("Reader result differs from dispatched range")
        entry = next((e for e in self.catalog if e["artifact_id"] == raw["artifact_id"]
                      and e["namespace"] == raw["namespace"] and e["work_id"] == raw["work_id"]), None)
        if entry is None or (entry["content_hash"] is not None and entry["content_hash"] != raw["sha256"]):
            raise ValueError("Reader result is outside authorized source/hash")
        card = self.request.cards[0]
        if not any(e.work_id == raw["work_id"] and e.evidence_id in card.evidence_ids
                   for e in self.request.evidence):
            raise ValueError("Reader Work does not belong to Card")
        audit = ReviewerReadAudit(read_id=raw["read_id"], namespace=raw["namespace"],
            work_id=raw["work_id"], artifact_id=raw["artifact_id"], artifact_hash=raw["sha256"],
            char_start=start, char_end=end, text_sha256=hashlib.sha256(raw["text"].encode()).hexdigest())
        previous = self.audits.get(audit.read_id)
        if previous is not None and previous != audit:
            raise ValueError("conflicting Reader receipt")
        self.audits[audit.read_id] = audit
        if start == end:
            return None
        digest = hashlib.sha256(f"{card.card_id}\x1f{audit.read_id}\x1f{start}\x1f{end}".encode()).hexdigest()
        item = ReviewEvidence(evidence_id="rev_ev_" + digest[:24],
            review_id="review_" + hashlib.sha256(card.card_id.encode()).hexdigest()[:20],
            origin_card_id=card.card_id, novelty_point_id=self.request.novelty_point.point_id,
            work_id=raw["work_id"], source_record_id=entry["source_record_id"],
            artifact_id=raw["artifact_id"], namespace=raw["namespace"], artifact_hash=raw["sha256"],
            read_id=audit.read_id, char_start=start, char_end=end, exact_quote=raw["text"],
            role=entry["role"], content_extent=entry["content_extent"], version_label=entry["version_label"])
        self.evidence[item.evidence_id] = item
        return item

    def choices(self):
        bound = set(self.request.cards[0].evidence_ids)
        result = {e.evidence_id: {"work_id": e.work_id, "origin": "card"}
                  for e in self.request.evidence if e.evidence_id in bound and e.quote}
        result.update({e.evidence_id: {"work_id": e.work_id, "origin": "reading"}
                       for e in self.evidence.values()})
        return result
