"""Explicit card handoffs and on-demand verification of selected evidence.

Reading ledgers stay in the data plane. This module neither retrieves new papers
nor compresses source text, and never equates a valid citation with true support.
"""
from __future__ import annotations

import hashlib

from .review_validation import feature_catalog
from .tool_call_harness import ToolCallBudgetExhausted
from ..schemas.domain import parse_card_review, ReviewerSupportReview
from ..schemas.reviewer_handoff import CardSummaryHandoff, CardJudgmentHandoff, SelectedEvidenceHandle


def card_handoff(row, review, evidence):
    if review.incomplete_reason in {"technical_error", "budget_exhausted", "material_unavailable"}:
        return CardSummaryHandoff(**{key: row[key] for key in
            ("index", "card_id", "novelty_point_id", "document_title")}, status="failed",
            error=f"单卡判断未完成：{review.incomplete_reason}")
    selected = list(dict.fromkeys(
        [eid for work in review.highly_relevant_works for eid in work.evidence_ids]
        + [eid for feature in review.feature_comparisons for eid in feature.evidence_refs]))
    handles = []
    for eid in selected:
        item = evidence.get(eid)
        if item is None:
            raise ValueError("handoff cites an unknown evidence_id")
        quote = getattr(item, "exact_quote", getattr(item, "quote", ""))
        if not quote:
            raise ValueError("handoff selected evidence has no retained source text")
        handles.append(SelectedEvidenceHandle(evidence_id=eid, work_id=item.work_id, card_id=row["card_id"]))
    judgment = CardJudgmentHandoff(
        review_status=review.status,
        support_status=review.support_status if isinstance(review, ReviewerSupportReview) else None,
        support_reason=review.support_reason if isinstance(review, ReviewerSupportReview) else (review.verdict_reason or ""),
        highly_relevant_works=review.highly_relevant_works,
        feature_comparisons=review.feature_comparisons,
        supplement_request=review.supplement_request, incomplete_reason=review.incomplete_reason,
        limitations=[issue.message for issue in review.execution_issues])
    return CardSummaryHandoff(**{key: row[key] for key in
        ("index", "card_id", "novelty_point_id", "document_title", "status")},
        review=judgment, selected_evidence=handles)


class SummaryVerification:
    """One synthesis invocation. Only requested, fully displayed sources authorize changes."""
    def __init__(self, request, rows, *, max_chars, max_requests):
        self.request, self.rows = request, rows
        self.max_chars, self.max_requests = max_chars, max_requests
        self.chars = 0
        self.records = []
        self.by_card = {row["card_id"]: row for row in rows}
        self.features = {f["feature_id"] for f in feature_catalog(request.novelty_point)}
        self.sources = {e.evidence_id: e for e in request.evidence}
        for row in rows:
            if row.get("status") == "completed":
                for e in parse_card_review(row["review"]).review_evidence:
                    if e.evidence_id in self.sources and self.sources[e.evidence_id] != e:
                        raise ValueError("conflicting summary evidence identity")
                    self.sources[e.evidence_id] = e

    def recheck(self, target):
        row = self.by_card.get(target.card_id)
        if row is None or row["status"] != "completed" or target.feature_id not in self.features:
            raise ValueError("summary recheck target is outside completed card/feature scope")
        handoff = CardSummaryHandoff.model_validate(row["handoff"])
        allowed = {e.evidence_id for e in handoff.selected_evidence if e.work_id == target.work_id}
        if not set(target.evidence_ids) <= allowed:
            raise ValueError("summary recheck evidence was not selected by this card and Work")
        if len(set(target.evidence_ids)) != len(target.evidence_ids):
            raise ValueError("duplicate summary recheck evidence")
        if len(self.records) >= self.max_requests:
            raise ToolCallBudgetExhausted("summary targeted verification request budget exhausted")
        quotes = []
        for eid in target.evidence_ids:
            item = self.sources[eid]
            quote = getattr(item, "exact_quote", getattr(item, "quote", ""))
            quotes.append({"evidence_id": eid, "work_id": item.work_id,
                "quote": quote, "quote_sha256": hashlib.sha256(quote.encode()).hexdigest()})
        count = sum(len(q["quote"]) for q in quotes)
        if not count or self.chars + count > self.max_chars:
            # Never silently truncate a source and authorize its complete evidence ID.
            raise ToolCallBudgetExhausted("summary targeted verification text budget exhausted")
        self.chars += count
        record = {**target.model_dump(mode="json"), "quotes": quotes,
                  "source": "retained_card_evidence", "new_reader_call": False}
        self.records.append(record)
        return record

    def validate(self, review):
        """Accept exact inherited comparisons or changes grounded in a targeted recheck.

        Free-form point reasoning remains semantic model work. This guard does not
        resolve the separately tracked point-level unknown/verdict policy.
        """
        origins = []
        handoffs = [CardSummaryHandoff.model_validate(row["handoff"]) for row in self.rows]
        for work in review.highly_relevant_works:
            permitted_for_work = set()
            for card_id in work.card_ids:
                handoff = next((h for h in handoffs if h.card_id == card_id), None)
                if handoff is None or not handoff.review:
                    raise ValueError("summary cites an incomplete card judgment")
                permitted = {e.evidence_id for e in handoff.selected_evidence if e.work_id == work.work_id}
                permitted_for_work.update(permitted)
                if not set(work.evidence_ids).intersection(permitted):
                    raise ValueError("summary Work cites evidence not selected by its card")
            if not set(work.evidence_ids) <= permitted_for_work:
                raise ValueError("summary Work includes evidence from an unlisted card")
        for item in review.feature_comparisons:
            candidates = [(h.card_id, f) for h in handoffs if h.review
                          for f in h.review.feature_comparisons
                          if (f.feature_id, f.work_id) == (item.feature_id, item.work_id)]
            matching = [cid for cid, f in candidates if f == item]
            relations = {(f.relation, f.basis_type, f.source_context) for _, f in candidates}
            checks = [r for r in self.records
                      if (r["feature_id"], r["work_id"]) == (item.feature_id, item.work_id)]
            verified = {q["evidence_id"] for r in checks for q in r["quotes"]}
            if matching and len(relations) <= 1:
                mode = "inherited"
            elif item.evidence_refs and set(item.evidence_refs) <= verified:
                mode = "rechecked"
            elif item.relation == "unknown" and item.basis_type == "insufficient" and not item.evidence_refs:
                mode = "unresolved"
            else:
                raise ValueError("new, changed or conflicting feature judgment requires targeted verification")
            origins.append({"feature_id": item.feature_id, "work_id": item.work_id,
                "mode": mode, "card_ids": matching if mode == "inherited" else sorted({
                    r["card_id"] for r in checks if set(r["evidence_ids"]).intersection(item.evidence_refs)
                }) if mode == "rechecked" else [],
                "evidence_refs": item.evidence_refs})
        return origins
