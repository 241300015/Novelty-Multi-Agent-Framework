"""Partition card judgments before Gate A; discarded material is audit-only."""
from __future__ import annotations

from ..schemas.domain import ReviewDisposition


def review_disposition(review, cards):
    ids = [c.card_id for c in cards]
    state = review.summary_harness_state
    if not state:
        return ReviewDisposition(selection_status="unavailable", candidate_card_ids=ids)
    discarded = state.get("discarded_card_ids", [])
    complete = state.get("stage") in {"draft", "complete"}
    selected = state.get("adopted_card_ids", []) if complete else [c for c in ids if c not in discarded]
    for values in (discarded, selected):
        if (not isinstance(values, list) or any(not isinstance(v, str) for v in values)
                or len(values) != len(set(values)) or not set(values) <= set(ids)):
            raise ValueError("summary adoption contains duplicate or out-of-scope cards")
    if set(selected) & set(discarded):
        raise ValueError("a card cannot be both adopted and discarded")
    # Unfinished/failed cards need not have been adopted. They stay outside the
    # active pool when selection completed, without being labelled model-discarded.
    return ReviewDisposition(selection_status="complete" if complete else "pending",
        candidate_card_ids=selected, discarded_card_ids=discarded)


def active_review(review, disposition, rows=(), *, cards):
    """Exclude inactive ledgers, including empty EOF receipts owned by card rows.

    Comparisons are never rewritten. Any remaining citation outside the active
    pool is rejected by Gate A, rather than silently repaired here.
    """
    active = set(disposition.candidate_card_ids)
    inactive = {c.card_id for c in cards} - active
    # Unknown ownership is illegal, not an inactive card: leave it for Gate A.
    evidence = [e for e in review.review_evidence if e.origin_card_id not in inactive]
    kept_reads = {e.read_id for e in evidence}
    removed_reads = {e.read_id for e in review.review_evidence if e.origin_card_id in inactive}
    for row in rows:
        if row.get("novelty_point_id") != review.novelty_point_id:
            continue
        raw = row.get("review")
        observations = raw.get("reader_observations", []) if isinstance(raw, dict) else []
        ids = {r["read_id"] for r in observations if isinstance(r, dict)
               and isinstance(r.get("read_id"), str)} if isinstance(observations, list) else set()
        if row.get("card_id") in active:
            kept_reads.update(ids)
        elif row.get("card_id") in inactive:
            removed_reads.update(ids)
    removed_reads -= kept_reads
    return review.model_copy(update={
        "review_evidence": evidence,
        "reader_observations": [r for r in review.reader_observations if r.read_id not in removed_reads],
        "read_citations": [r for r in review.read_citations if r.read_id not in removed_reads],
        "report_disposition": disposition,
    })


def pool_payloads(reviews, cards, rows):
    """Persistence views; no source-file reads and no scientific reclassification."""
    active, discarded = set(), set()
    for review in reviews:
        d = review.report_disposition
        if d is not None:
            active.update((review.novelty_point_id, cid) for cid in d.candidate_card_ids)
            discarded.update((review.novelty_point_id, cid) for cid in d.discarded_card_ids)
    def payload(keys):
        return {"cards": [c.model_dump(mode="json") for c in cards if (c.novelty_point_id, c.card_id) in keys],
                "card_reviews": [r for r in rows or [] if (r.get("novelty_point_id"), r.get("card_id")) in keys]}
    candidates = payload(active)
    candidates["reviews"] = [r.model_dump(mode="json") for r in reviews]
    unused = payload(discarded)
    unused["audit_only"] = True
    # A failed/unadopted card is not the same as a deliberate discard.
    unused["unselected_card_reviews"] = [r for r in rows or []
        if (r.get("novelty_point_id"), r.get("card_id")) not in active | discarded]
    return candidates, unused
