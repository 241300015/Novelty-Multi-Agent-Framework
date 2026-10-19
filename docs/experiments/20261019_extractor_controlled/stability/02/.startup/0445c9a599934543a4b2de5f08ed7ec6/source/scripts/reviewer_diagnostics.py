"""只读检查 Reviewer 产物、引用闭包与 Runtime Artifact。

用法：
    python scripts/reviewer_diagnostics.py outputs/<paper_id>

脚本只输出 ID、计数和错误，不复制 Evidence quote 或 Reader 正文。
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "backend" / "src"))

from pydantic import ValidationError

from novelty_agent_framework.schemas import NoveltyPointReview


def _load_json(path: Path, errors: list[str]) -> Any:
    if not path.is_file():
        errors.append(f"missing file: {path.name}")
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        errors.append(f"invalid JSON {path.name}: {type(exc).__name__}: {exc}")
        return {}


def _review_evidence_index(
    review: NoveltyPointReview,
    card_by_id: dict[str, dict[str, Any]],
    evidence_by_id: dict[str, dict[str, Any]],
    errors: list[str],
) -> dict[str, dict[str, Any]]:
    """Index this review's additions only after checking their original scope.

    This checks reference closure, not Reader provenance or source quote integrity.
    Original Card evidence is deliberately resolved from research attempts only.
    """
    counts = Counter(item.evidence_id for item in review.review_evidence)
    additions: dict[str, dict[str, Any]] = {}
    for item in review.review_evidence:
        evidence_id = item.evidence_id
        if counts[evidence_id] > 1:
            errors.append(f"duplicate ReviewEvidence ID: {evidence_id}")
            continue
        if evidence_id in evidence_by_id:
            errors.append(f"ReviewEvidence shadows original Evidence: {evidence_id}")
            continue
        card = card_by_id.get(item.origin_card_id)
        if card is None:
            errors.append(
                f"ReviewEvidence references missing Card: {item.origin_card_id}"
            )
            continue
        if item.novelty_point_id != review.novelty_point_id or card.get(
            "novelty_point_id"
        ) != review.novelty_point_id:
            errors.append(f"cross-point ReviewEvidence: {evidence_id}")
            continue
        if not any(
            (original := evidence_by_id.get(original_id)) is not None
            and original.get("work_id") == item.work_id
            and original.get("novelty_point_id") == review.novelty_point_id
            for original_id in card.get("evidence_ids", [])
        ):
            errors.append(f"ReviewEvidence outside Card/Work scope: {evidence_id}")
            continue
        additions[evidence_id] = item.model_dump(mode="json")
    return additions


def inspect_workspace(workspace: Path, *, run_id: str | None = None) -> dict[str, Any]:
    """Return a deterministic, source-text-free Reviewer diagnostic report."""

    workspace = Path(workspace)
    errors: list[str] = []
    warnings: list[str] = []
    points_payload = _load_json(workspace / "novelty-points.json", errors)
    cards_payload = _load_json(workspace / "evidence-cards.json", errors)
    reviews_payload = _load_json(workspace / "novelty-reviews.json", errors)

    point_ids = [
        item.get("point_id")
        for item in points_payload.get("novelty_points", [])
        if isinstance(item, dict) and isinstance(item.get("point_id"), str)
    ]
    cards = cards_payload.get(
        "validator_accepted_cards",
        cards_payload.get("accepted_evidence_cards", []),
    )
    cards = [item for item in cards if isinstance(item, dict)]
    card_by_id = {
        item["card_id"]: item
        for item in cards
        if isinstance(item.get("card_id"), str)
    }

    evidence_by_id: dict[str, dict[str, Any]] = {}
    research_files = sorted((workspace / "research-runs").glob("**/attempt-*.json"))
    for path in research_files:
        payload = _load_json(path, errors)
        for item in payload.get("evidence", []):
            if not isinstance(item, dict) or not isinstance(
                item.get("evidence_id"), str
            ):
                continue
            evidence_id = item["evidence_id"]
            previous = evidence_by_id.get(evidence_id)
            if previous is not None and previous != item:
                errors.append(f"conflicting Evidence ID: {evidence_id}")
            evidence_by_id[evidence_id] = item

    unresolved_evidence_ids = sorted(
        {
            evidence_id
            for card in cards
            for evidence_id in card.get("evidence_ids", [])
            if evidence_id not in evidence_by_id
            or evidence_by_id[evidence_id].get("novelty_point_id")
            != card.get("novelty_point_id")
        }
    )
    errors.extend(
        f"Card references missing Evidence: {evidence_id}"
        for evidence_id in unresolved_evidence_ids
    )

    raw_reviews = reviews_payload.get("reviews", [])
    reviews: list[NoveltyPointReview] = []
    for index, item in enumerate(raw_reviews):
        try:
            reviews.append(NoveltyPointReview.model_validate(item))
        except ValidationError as exc:
            errors.append(
                f"invalid review at index {index}: "
                f"{exc.errors(include_url=False)[0]['msg']}"
            )
    review_ids = [item.novelty_point_id for item in reviews]
    counts = Counter(review_ids)
    missing_review_ids = sorted(set(point_ids) - set(review_ids))
    unexpected_review_ids = sorted(set(review_ids) - set(point_ids))
    duplicate_review_ids = sorted(
        point_id for point_id, count in counts.items() if count > 1
    )
    errors.extend(f"missing review: {point_id}" for point_id in missing_review_ids)
    errors.extend(
        f"unexpected review: {point_id}" for point_id in unexpected_review_ids
    )
    errors.extend(
        f"duplicate review: {point_id}" for point_id in duplicate_review_ids
    )

    registered_evidence_ids: set[str] = set()
    for review in reviews:
        additions = _review_evidence_index(review, card_by_id, evidence_by_id, errors)
        registered_evidence_ids.update(additions)
        for work in review.highly_relevant_works:
            for card_id in work.card_ids:
                card = card_by_id.get(card_id)
                if card is None:
                    errors.append(f"review references missing Card: {card_id}")
                    continue
                if review.novelty_point_id != card.get("novelty_point_id"):
                    errors.append(f"cross-point Card reference: {card_id}")
            for evidence_id in work.evidence_ids:
                evidence = evidence_by_id.get(evidence_id) or additions.get(evidence_id)
                if evidence is None:
                    errors.append(
                        f"review references missing Evidence: {evidence_id}"
                    )
                elif evidence.get("work_id") != work.work_id:
                    errors.append(
                        f"Work/Evidence mismatch: {work.work_id}/{evidence_id}"
                    )
                elif evidence.get("novelty_point_id") != review.novelty_point_id:
                    errors.append(f"cross-point Evidence reference: {evidence_id}")
            cited_ids = set(work.evidence_ids)
            for card_id in work.card_ids:
                card = card_by_id.get(card_id)
                if card is None:
                    continue
                linked_ids = set(card.get("evidence_ids", [])) | {
                    evidence_id
                    for evidence_id, item in additions.items()
                    if item["origin_card_id"] == card_id
                }
                if not cited_ids.intersection(linked_ids):
                    errors.append(
                        f"Card/Evidence link missing in RelevantWork: {card_id}"
                    )

    supplement_point_ids = sorted(
        item.novelty_point_id
        for item in reviews
        if item.supplement_request is not None
    )
    if supplement_point_ids:
        warnings.append(
            "supplement_request is informational and does not control V0 routing"
        )

    runtime_root = workspace / "runtime"
    stage_pattern = (
        f"{run_id}/stages/*_review_evidence/meta.json"
        if run_id is not None
        else "*/stages/*_review_evidence/meta.json"
    )
    runtime_stage_files = sorted(runtime_root.glob(stage_pattern))
    runtime_checks = []
    for path in runtime_stage_files:
        payload = _load_json(path, errors)
        details = payload.get("debug_details", {}).get(
            "reviewer_information_adjudication"
        )
        if isinstance(details, dict):
            runtime_checks.append(
                {
                    "path": str(path.relative_to(workspace)),
                    "cards_preserved": details.get("cards_preserved"),
                    "missing_review_point_ids": details.get(
                        "missing_review_point_ids", []
                    ),
                    "unresolved_evidence_ids": details.get(
                        "unresolved_evidence_ids", []
                    ),
                }
            )

    reader_calls = []
    reader_pattern = (
        f"{run_id}/tools/*_reader.json"
        if run_id is not None
        else "*/tools/*_reader.json"
    )
    reader_call_files = sorted(runtime_root.glob(reader_pattern))
    for path in reader_call_files:
        payload = _load_json(path, errors)
        if payload.get("stage_name") != "review_evidence":
            continue
        resolved = payload.get("resolved_arguments") or {}
        error = payload.get("error") or {}
        reader_calls.append(
            {
                "path": str(path.relative_to(workspace)),
                "execution_status": payload.get("execution_status"),
                "artifact_id": resolved.get("artifact_id"),
                "char_start": resolved.get("char_start"),
                "max_chars": resolved.get("max_chars"),
                "error_type": error.get("type"),
                "error_message": error.get("message"),
            }
        )

    unique_errors = list(dict.fromkeys(errors))
    return {
        "workspace": str(workspace),
        "run_id": run_id,
        "ok": not unique_errors,
        "counts": {
            "novelty_points": len(point_ids),
            "validator_accepted_cards": len(cards),
            "evidence": len(evidence_by_id) + len(registered_evidence_ids),
            "reviews": len(reviews),
            "runtime_review_stages": len(runtime_checks),
            "reviewer_reader_calls": len(reader_calls),
        },
        "missing_review_point_ids": missing_review_ids,
        "unexpected_review_point_ids": unexpected_review_ids,
        "duplicate_review_point_ids": duplicate_review_ids,
        "unresolved_evidence_ids": unresolved_evidence_ids,
        "supplement_point_ids": supplement_point_ids,
        "runtime_checks": runtime_checks,
        "reader_calls": reader_calls,
        "errors": unique_errors,
        "warnings": warnings,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("workspace", type=Path, help="outputs/<paper_id> 目录")
    parser.add_argument("--run-id", help="只检查指定 Runtime run 的 stage/tool 记录")
    parser.add_argument("--compact", action="store_true", help="输出单行 JSON")
    args = parser.parse_args()
    report = inspect_workspace(args.workspace, run_id=args.run_id)
    print(
        json.dumps(
            report,
            ensure_ascii=False,
            indent=None if args.compact else 2,
            sort_keys=True,
        )
    )
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
