"""Runtime read receipts and authorization of explicitly supplied evidence.

Readers remain deterministic content adapters. These helpers never read files
or trust model-written caller identities.
"""
from __future__ import annotations

import uuid


def record_read_receipts(observation, *, caller_role, invocation_id, call_id, scope, run_id=None):
    rows = observation.payload.get("read_results") or [observation.payload.get("read_result", {})]
    if not observation.succeeded:
        rows = []
    receipts = []
    for row in rows:
        if not row.get("read_id") or not row.get("text"):
            continue
        receipts.append({
            "receipt_id": "receipt_" + uuid.uuid4().hex,
            "read_id": row["read_id"],
            "caller": {"role": caller_role, "invocation_id": invocation_id,
                       "run_id": getattr(scope, "run_id", None) or run_id,
                       "paper_id": getattr(scope, "subject_paper_id", None),
                       "point_id": getattr(getattr(scope, "novelty_point", None), "point_id", None),
                       "task_id": getattr(getattr(scope, "research_task", None), "task_id", None),
                       "card_ids": [c.card_id for c in getattr(scope, "cards", [])]},
            "call_id": call_id, "namespace": row.get("namespace"),
            "artifact_id": row.get("artifact_id"), "material_id": row.get("material_id"),
            "sha256": row.get("sha256"),
            "char_start": row.get("char_start"), "char_end": row.get("char_end"),
        })
    return observation.model_copy(update={"payload": {**observation.payload, "read_receipts": receipts}})


def supplied_evidence_for_read(evidence, read_id, *, char_start=None, char_end=None):
    """Resolve only an already supplied quote; never authorize its entire old read.

The historical read ID is a compatibility alias, not evidence of a new read.
Ambiguous aliases and invented ranges fail closed.
"""
    candidates = [item for item in evidence if item.provenance.get("read_id") == read_id]
    if char_start is not None or char_end is not None:
        candidates = [item for item in candidates
                      if (char_start is None or item.locator.char_start == char_start)
                      and (char_end is None or item.locator.char_end == char_end)]
    if len(candidates) != 1:
        raise ValueError(f"unread or ambiguous read_id {read_id}: no unique authorized input quote")
    return candidates[0]


def attach_evidence_receipts(evidence, trace):
    receipts = {}
    for event in trace:
        observation = getattr(event, "observation", None)
        if observation is not None:
            for receipt in observation.payload.get("read_receipts", []):
                receipts.setdefault(receipt["read_id"], []).append(receipt)
    return [item.model_copy(update={"provenance": {**item.provenance,
            "read_receipts": receipts[item.provenance["read_id"]]}})
            if item.provenance.get("read_id") in receipts else item for item in evidence]
