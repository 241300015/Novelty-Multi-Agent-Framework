"""Literal retention checks for extraction; never infer technical equivalence.

A coverage ledger distinguishes data preservation from source support and from
independent-contribution judgement. Unknown semantic relations remain pending.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from typing import Any

from ..schemas import NoveltyPoint, PaperDigest

FEATURE_FIELDS = ("technical_features", "technical_features_en")


def _key(value: str) -> str:
    return value.strip()  # No case folding, stemming, keyword or embedding match.


def _union(values: Sequence[str]) -> list[str]:
    seen: set[str] = set()
    result = []
    for value in values:
        if _key(value) not in seen:
            seen.add(_key(value))
            result.append(value)
    return result


def merge_same_claim(points: Sequence[NoveltyPoint]) -> list[NoveltyPoint]:
    """Preserve all literal features/locations from repeated exact claims."""
    merged: dict[str, NoveltyPoint] = {}
    for point in points:
        key = _key(point.claim)
        if key not in merged:
            merged[key] = point
            continue
        previous = merged[key]
        merged[key] = previous.model_copy(update={
            **{field: _union([*getattr(previous, field), *getattr(point, field)])
               for field in (*FEATURE_FIELDS, "source_locations")},
            "claim_en": previous.claim_en or point.claim_en,
        })
    return list(merged.values())


def literal_coverage(source: NoveltyPoint, target: NoveltyPoint) -> dict[str, Any]:
    missing = {field: [value for value in getattr(source, field)
        if _key(value) and _key(value) not in {_key(item) for item in getattr(target, field)}]
        for field in FEATURE_FIELDS}
    claim_equal = _key(source.claim) == _key(target.claim)
    english_preserved = not _key(source.claim_en) or _key(source.claim_en) == _key(target.claim_en)
    features_present = any(_key(item) for field in FEATURE_FIELDS for item in getattr(source, field))
    return {"claim_equal": claim_equal, "english_claim_preserved": english_preserved,
        "features_present": features_present, "missing_features": missing,
        "literal_coverage_proven": claim_equal and english_preserved and features_present and not any(missing.values()),
        "semantic_equivalence_verified": False}


def guard_deletions(points: Sequence[NoveltyPoint], audit: Mapping[str, Any], *, enabled: bool) -> dict[str, Any]:
    accepted, pending, issues = [], set(audit["pending_indices"]), []
    proposals = list(audit["accepted_deletions"])
    for proposal in proposals:
        proof = literal_coverage(points[proposal["index"] - 1], points[proposal["duplicate_of"] - 1])
        checked = {**proposal, "coverage_proof": proof}
        if not enabled or proof["literal_coverage_proven"]:
            accepted.append(checked)
        else:
            pending.add(proposal["index"])
            issues.append({"index": proposal["index"], "duplicate_of": proposal["duplicate_of"],
                "reason": "literal_coverage_not_proven", "coverage_proof": proof})
    return {**audit, "contract_valid_deletion_proposals": proposals,
        "accepted_deletions": accepted, "pending_indices": sorted(pending),
        "coverage_issues": issues, "conservative_dedup": enabled}


def _source_units(digest: PaperDigest) -> list[dict[str, Any]]:
    rows = [{"source": f"claimed_contributions[{i}]", "text": text, "kind": "author_claim"}
            for i, text in enumerate(digest.claimed_contributions, 1)]
    markers = list(re.finditer(r"\[作者贡献段 (\d+)\]\n", digest.full_text_excerpt))
    for i, marker in enumerate(markers):
        stop = markers[i + 1].start() if i + 1 < len(markers) else len(digest.full_text_excerpt)
        rows.append({"source": f"full_text_excerpt[{marker.end()}:{stop}]",
            "text": digest.full_text_excerpt[marker.end():stop], "kind": "bounded_author_excerpt"})
    for row in rows:
        row["source_id"] = hashlib.sha256(json.dumps(row, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    return rows


def build_coverage_ledger(digest: PaperDigest, trace: Mapping[str, Any],
                          final_points: Sequence[NoveltyPoint], *, conservative: bool) -> dict[str, Any]:
    """Map every recorded candidate/feature to a final point or explicit unknown."""
    occurrences = [(f"generation:{generation_index}:{index}", point, True)
        for generation_index, generation in enumerate(trace.get("generation", []), 1)
        for index, point in enumerate(generation.get("candidates", []), 1)]
    occurrences.extend((f"coverage:{index}", point, True)
        for index, point in enumerate(trace.get("coverage_followup_candidates", []), 1))
    for generation_index, generation in enumerate(trace.get("generation", []), 1):
        if generation.get("validation_error") and isinstance(generation.get("raw_candidates"), list):
            occurrences.extend((f"generation:{generation_index}:{index}", raw, False)
                for index, raw in enumerate(generation["raw_candidates"], 1))
    if trace.get("coverage_followup_error") and isinstance(trace.get("coverage_followup_raw_candidates"), list):
        occurrences.extend((f"coverage:{index}", raw, False)
            for index, raw in enumerate(trace["coverage_followup_raw_candidates"], 1))
    # A semantic proposal may have been executed in legacy mode. Record its
    # target, but do not turn that claimed relationship into literal coverage.
    audit = trace.get("deduplication", {})
    merged = trace.get("merged_candidates", [])
    representatives = {}
    pending_claims = set()
    for item in audit.get("accepted_deletions", []):
        representatives[_key(merged[item["index"] - 1]["claim"])] = _key(merged[item["duplicate_of"] - 1]["claim"])
    for index in audit.get("pending_indices", []):
        if 1 <= index <= len(merged):
            pending_claims.add(_key(merged[index - 1]["claim"]))
    candidates, features = [], []
    for candidate_id, raw, validated in occurrences:
        if not validated:
            candidates.append({"candidate_id": candidate_id, "original": raw,
                "original_sha256": hashlib.sha256(json.dumps(raw, ensure_ascii=False, sort_keys=True).encode()).hexdigest(),
                "final_point_ids": [], "status": "pending_invalid_candidate_batch",
                "dedup_pending": False, "semantic_equivalence_verified": False})
            if isinstance(raw, Mapping):
                for field in FEATURE_FIELDS:
                    values = raw.get(field)
                    if isinstance(values, list):
                        features.extend({"feature_id": f"{candidate_id}:{field}:{index}",
                            "candidate_id": candidate_id, "field": field, "text": text,
                            "final_point_ids": [], "status": "pending_invalid_candidate_batch"}
                            for index, text in enumerate(values, 1) if isinstance(text, str) and _key(text))
            continue
        source = NoveltyPoint.model_validate(raw)
        key = _key(source.claim)
        target_key = representatives.get(key, key)
        targets = [point for point in final_points if _key(point.claim) == target_key]
        proofs = [literal_coverage(source, point) for point in targets]
        candidate_features = []
        for field in FEATURE_FIELDS:
            for index, text in enumerate(getattr(source, field), 1):
                if not _key(text):
                    continue
                ids = [point.point_id for point in targets if _key(text) in {_key(v) for v in getattr(point, field)}]
                row = {"feature_id": f"{candidate_id}:{field}:{index}", "candidate_id": candidate_id,
                    "field": field, "text": text, "final_point_ids": ids,
                    "status": "retained_literal" if ids else "pending_feature_loss"}
                features.append(row)
                candidate_features.append(row)
        preserved = bool(proofs) and any(proof["literal_coverage_proven"] for proof in proofs)
        status = ("pending_technical_features" if not candidate_features else
                  "retained_literal" if preserved else
                  "pending_semantic_mapping" if targets and all(row["final_point_ids"] for row in candidate_features)
                  else "pending_feature_loss" if targets else "pending_unmapped_candidate")
        candidates.append({"candidate_id": candidate_id, "original": raw,
            "original_sha256": hashlib.sha256(json.dumps(raw, ensure_ascii=False, sort_keys=True).encode()).hexdigest(),
            "final_point_ids": [p.point_id for p in targets], "status": status,
            "dedup_pending": key in pending_claims,
            "semantic_equivalence_verified": False})
    sources = _source_units(digest)
    for source in sources:
        literal_ids = [point.point_id for point in final_points if _key(source["text"]) and _key(source["text"]) in
            {_key(point.claim), _key(point.claim_en), *(_key(v) for field in FEATURE_FIELDS for v in getattr(point, field))}]
        source.update(final_point_ids=literal_ids,
            status="literal_text_retained" if literal_ids else "pending_source_alignment",
            semantic_support_verified=False)
    point_statuses = [{"point_id": point.point_id,
        "status": ("pending_technical_features" if not any(_key(v) for field in FEATURE_FIELDS for v in getattr(point, field))
                   else "pending_dedup" if _key(point.claim) in pending_claims else "candidate_for_research"),
        "independent_contribution_confirmed": False} for point in final_points]
    generation_errors = list(trace.get("generation_errors", []))
    complete = not generation_errors and not trace.get("coverage_followup_error") and bool(candidates) and bool(sources) and all(
        row["status"] == "retained_literal" and not row["dedup_pending"] for row in candidates) and all(
        row["status"] == "literal_text_retained" for row in sources)
    return {"schema_version": "1.0", "scope": "provided_digest_and_recorded_candidates_only",
        "conservative_dedup": conservative, "candidates": candidates, "features": features,
        "source_units": sources, "final_points": point_statuses,
        "candidate_features_preserved": all(row["final_point_ids"] for row in features),
        "source_coverage_status": ("unknown_no_explicit_author_units" if not sources else
                                   "literal_text_retained" if all(row["final_point_ids"] for row in sources)
                                   else "pending_source_alignment"),
        "coverage_complete": complete, "semantic_coverage_verified": False,
        "unresolved_generation_errors": generation_errors,
        "coverage_followup_error": trace.get("coverage_followup_error"),
        "independent_contributions_confirmed": False,
        "unseen_full_text_coverage_verified": False}
