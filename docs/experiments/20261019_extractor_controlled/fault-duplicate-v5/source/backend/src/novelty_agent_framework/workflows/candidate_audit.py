"""Deterministic candidate dispositions derived from observations, not model claims."""
from __future__ import annotations

from ..schemas.research import CandidateAuditRecord


def _mapping(value):
    return value if isinstance(value, dict) else {}


def _rows(value):
    return [row for row in value if isinstance(row, dict)] if isinstance(value, list) else []


def build_candidate_audit(trace, reads, evidence, cards, *, interrupted=False):
    candidates = {}

    def add(namespace, row, artifact_ids=()):
        if not isinstance(row, dict):
            return None
        row = {key: value for key, value in row.items() if isinstance(value, str)}
        work_id, source_id = row.get("work_id"), row.get("source_record_id")
        if not work_id and not source_id:
            return None
        key = (namespace, "work:" + work_id if work_id else "source:" + source_id)
        old_key = (namespace, "source:" + source_id) if source_id else None
        if old_key != key and old_key in candidates:
            old = candidates.pop(old_key)
        else:
            old = None
        item = candidates.setdefault(key, old or {
            "namespace": namespace, "work_id": work_id, "source_record_id": source_id,
            "title": row.get("title"), "artifact_ids": [], "read_ids": [], "card_ids": [],
        })
        if old is not None and old is not item:
            for field in ("artifact_ids", "read_ids", "card_ids"):
                item[field] = list(dict.fromkeys([*item[field], *old[field]]))
            if not item["title"]:
                item["title"] = old["title"]
        for field in ("work_id", "source_record_id", "title"):
            if row.get(field):
                item[field] = row[field]
        item["artifact_ids"] = list(dict.fromkeys([*item["artifact_ids"], *[value for value in (artifact_ids or []) if isinstance(value, str)]]))
        return item

    for event in trace:
        observation = event.observation
        if observation is None or event.kind != "tool_result":
            continue
        payload = observation.payload
        for row in _rows(payload.get("target_exclusions")):
            key = (row.get("namespace", "research_reference"),
                   "work:" + row["work_id"] if row.get("work_id") else
                   "source:" + str(row.get("source_record_id")))
            candidates[key] = {
                "namespace": row.get("namespace", "research_reference"),
                "work_id": row.get("work_id"),
                "source_record_id": row.get("source_record_id"),
                "title": row.get("title"),
                "artifact_ids": [], "read_ids": [], "card_ids": [],
                "status": "excluded", "reason": "Target paper identity gate excluded this candidate.",
                "excluded_reason": "target_paper", "identity_match": row.get("identity_match"),
            }
        bundle = _mapping(payload.get("research_bundle") or payload.get("bundle"))
        # Discovery facts remain auditable even if the search observation failed.
        for record in [*_rows(bundle.get("source_records")), *_rows(payload.get("source_records"))]:
            if not isinstance(record, dict):
                continue
            ids = [a["artifact_id"] for a in _rows(bundle.get("artifacts"))
                   if a.get("work_id") == record.get("work_id") and a.get("artifact_id")]
            add("research_reference", record, ids)
        for row in _rows(_mapping(payload.get("database_search_result")).get("results")):
            add("research_reference", row, row.get("artifact_ids", []))
        for row in _rows(_mapping(payload.get("search_result")).get("results")):
            add("research_reference", row)
        browser = _mapping(payload.get("browser_result"))
        if observation.succeeded and browser:
            add("research_reference", browser, [a["artifact_id"] for a in _rows(browser.get("artifacts"))])
        for row in _rows(_mapping(payload.get("reference_search_result")).get("results")):
            add("subject_reference", row, [h["artifact_id"] for h in _rows(row.get("artifact_handles"))])

    for read in reads:
        item = add(read.namespace.value, {"work_id": read.work_id}, [read.artifact_id])
        item["read_ids"] = list(dict.fromkeys([*item["read_ids"], read.read_id]))
    evidence_by_id = {item.evidence_id: item for item in evidence}
    for card in cards:
        for evidence_id in card.evidence_ids:
            item = evidence_by_id[evidence_id]
            namespace = item.provenance.get("artifact_namespace", "research_reference")
            record = add(namespace, {"work_id": item.work_id, "title": card.document_title})
            record["card_ids"] = list(dict.fromkeys([*record["card_ids"], card.card_id]))

    rows = []
    for item in candidates.values():
        if item.get("status") == "excluded":
            rows.append(CandidateAuditRecord(**item))
            continue
        if item["card_ids"]:
            status, reason = "card_produced", "Builder produced grounded evidence; final validation is separate."
        elif item["read_ids"]:
            status = "read_task_interrupted" if interrupted else "read_without_card"
            reason = ("Task interrupted; no valid card was returned for this work." if interrupted else
                      "No valid card selected or built; no per-work irrelevance claim is inferred.")
        elif not item["artifact_ids"]:
            status, reason = "acquisition_unavailable", "Discovery only; no readable artifact observed."
        else:
            status, reason = "not_read", "Readable candidate was not read before this task ended."
        rows.append(CandidateAuditRecord(**item, status=status, reason=reason))
    return rows
