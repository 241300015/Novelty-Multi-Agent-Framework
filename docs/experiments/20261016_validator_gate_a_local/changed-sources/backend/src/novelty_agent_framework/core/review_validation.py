"""Shared deterministic Review contracts; no model or Agent dependency."""
from __future__ import annotations

import hashlib

from ..schemas import NoveltyPointReview, NoveltyPointReviewRequest


def feature_catalog(point) -> list[dict[str, str]]:
    return [{"feature_id": f"F{i}", "text": value,
             "sha256": hashlib.sha256(value.encode()).hexdigest()}
            for i, value in enumerate(point.technical_features, 1)]



def validate_review_references(
    review: NoveltyPointReview,
    request: NoveltyPointReviewRequest,
) -> NoveltyPointReview:
    if review.novelty_point_id != request.novelty_point.point_id:
        raise ValueError("review novelty_point_id is outside request scope")
    cards = {item.card_id: item for item in request.cards}
    evidence = {item.evidence_id: item for item in request.evidence}
    additions = {item.evidence_id: item for item in review.review_evidence}
    if len(additions) != len(review.review_evidence):
        raise ValueError("duplicate review evidence ID")
    if set(additions).intersection(evidence):
        raise ValueError("review evidence ID collides with original Evidence")
    for item in additions.values():
        card = cards.get(item.origin_card_id)
        if card is None or item.novelty_point_id != request.novelty_point.point_id \
                or not any(e.work_id == item.work_id and e.evidence_id in card.evidence_ids
                           for e in request.evidence):
            raise ValueError("review evidence is outside Card and Work scope")
    for work in review.highly_relevant_works:
        if any(card_id not in cards for card_id in work.card_ids):
            raise ValueError("relevant work cites an unknown card_id")
        cited_evidence = []
        for evidence_id in work.evidence_ids:
            item = evidence.get(evidence_id) or additions.get(evidence_id)
            if item is None:
                raise ValueError("relevant work cites an unknown evidence_id")
            if item.work_id != work.work_id:
                raise ValueError("relevant work_id does not match cited evidence")
            cited_evidence.append(evidence_id)
        if any(not (set(cards[card_id].evidence_ids) |
                    {item.evidence_id for item in additions.values() if item.origin_card_id == card_id}
                   ).intersection(cited_evidence) for card_id in work.card_ids):
            raise ValueError("relevant work card_ids are not linked to cited evidence")
    feature_ids = {item["feature_id"] for item in feature_catalog(request.novelty_point)}
    authorized_works = {item.work_id for item in request.evidence
                        if any(item.evidence_id in card.evidence_ids for card in request.cards)}
    for item in review.feature_comparisons:
        if item.feature_id not in feature_ids:
            raise ValueError("feature comparison cites an unknown feature_id")
        if item.work_id not in authorized_works:
            raise ValueError("feature comparison Work is outside request scope")
        if item.relation != "unknown" and not item.evidence_refs:
            raise ValueError("non-unknown feature comparison requires evidence")
        for evidence_id in item.evidence_refs:
            cited = evidence.get(evidence_id) or additions.get(evidence_id)
            if cited is None or cited.work_id != item.work_id:
                raise ValueError("feature comparison evidence is missing or from another Work")
    return review


def has_complete_work_coverage(review, point) -> bool:
    """One Work must support every declared feature with a unique cited row."""
    expected = {item["feature_id"] for item in feature_catalog(point)}
    by_work = {}
    for comparison in review.feature_comparisons:
        by_work.setdefault(comparison.work_id, {}).setdefault(comparison.feature_id, []).append(comparison)
    return any(expected and all(
        len(rows.get(fid, [])) == 1 and rows[fid][0].relation == "supported"
        and rows[fid][0].evidence_refs for fid in expected
    ) for rows in by_work.values())
