"""Offline handoff audit of saved card results; never runs a model or changes a checkpoint.

Usage: PYTHONPATH=backend/src:. python scripts/audit_reviewer_summary_handoff.py
       --checkpoint .../checkpoint.json --prior-call .../llm_calls/0050_....json
       --output .../handoff-audit.json
Repeat checkpoint/prior-call pairs to compare several archived attempts.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from novelty_agent_framework.agents.evidence_reviewer import (
    _compact_summary_rows, _summary_model_rows, _verify_summary_payload,
)
from novelty_agent_framework.core.reviewer_summary import SummaryVerification
from novelty_agent_framework.schemas import NoveltyPointReview, NoveltyPointReviewRequest


def audit(checkpoint_path, prior_call_path):
    checkpoint_bytes = checkpoint_path.read_bytes()
    call_bytes = prior_call_path.read_bytes()
    checkpoint, call = json.loads(checkpoint_bytes), json.loads(call_bytes)
    request = NoveltyPointReviewRequest.model_validate(checkpoint["request"])
    original = json.loads(call["request_payload"]["messages"][1]["content"])
    if original["novelty_point"]["point_id"] != request.novelty_point.point_id:
        raise ValueError("comparison point mismatch")
    rows, ids = _compact_summary_rows(request, checkpoint["card_results"])
    handoffs = _summary_model_rows(rows)
    if [r["card_id"] for r in original["card_reviews"]] != [r["card_id"] for r in handoffs]:
        raise ValueError("comparison card mismatch")
    _verify_summary_payload(rows, json.dumps({"card_reviews": handoffs}))
    # Validate inherited references/relations, without manufacturing a scientific verdict.
    inherited = NoveltyPointReview(novelty_point_id=request.novelty_point.point_id,
        status="insufficient_evidence", feature_comparisons=[f for row in handoffs if row["review"]
            for f in row["review"]["feature_comparisons"]])
    origins = SummaryVerification(request, rows, max_chars=32000, max_requests=6).validate(inherited)
    old_refs = {q["evidence_id"] for row in original["card_reviews"] for q in row.get("key_quotes", [])
                if q.get("cited_by_card")}
    if old_refs != ids:
        raise ValueError("selected evidence changed between archived call and checkpoint")
    audit_result = {
        "point_id": request.novelty_point.point_id,
        "checkpoint": str(checkpoint_path), "checkpoint_sha256": hashlib.sha256(checkpoint_bytes).hexdigest(),
        "prior_call": str(prior_call_path), "prior_call_sha256": hashlib.sha256(call_bytes).hexdigest(),
        "character_metric": "len(json.dumps(card_reviews, ensure_ascii=False)); not tokens",
        "before": {"handoff_characters": len(json.dumps(original["card_reviews"], ensure_ascii=False)),
            "displayed_source_characters": sum(len(q["quote"]) for r in original["card_reviews"] for q in r.get("key_quotes", [])),
            "omitted_selected_evidence_ids": [q["evidence_id"] for r in original["card_reviews"]
                for q in r.get("key_quotes", []) if q.get("cited_by_card") and not q["quote"]]},
        "after": {"handoff_characters": len(json.dumps(handoffs, ensure_ascii=False)),
            "displayed_source_characters": 0, "selected_evidence_ids": sorted(ids),
            "retained_ledger_entries": sum(len(r.get("review_evidence", [])) for r in rows),
            "comparison_origins": origins},
        "checkpoint_unchanged": checkpoint_path.read_bytes() == checkpoint_bytes,
        "new_model_calls": 0, "new_scientific_verdict": None,
    }
    return audit_result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checkpoint", type=Path, action="append", required=True)
    parser.add_argument("--prior-call", type=Path, action="append", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if len(args.checkpoint) != len(args.prior_call):
        parser.error("provide one prior-call for each checkpoint")
    result = {"validation": "offline handoff reconstruction; no model/backend validation",
              "attempts": [audit(c, p) for c, p in zip(args.checkpoint, args.prior_call)]}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"attempts": len(result["attempts"]), "output": str(args.output), "model_calls": 0}))


if __name__ == "__main__":
    main()
