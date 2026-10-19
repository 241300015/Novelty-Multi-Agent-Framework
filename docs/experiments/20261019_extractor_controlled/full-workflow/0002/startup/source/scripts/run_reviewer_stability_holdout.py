"""Two pre-frozen local Reviewer-only holdouts sharing the stability budget."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from backend.env import ModelCallOptions
from novelty_agent_framework.agents.evidence_reviewer import EvidenceReviewerConfig, NoveltyEvidenceReviewer
from novelty_agent_framework.config.factory import build_model_registry, build_prompt_library
from novelty_agent_framework.config.loader import load_application_config
from novelty_agent_framework.core.runtime_artifacts import RuntimeArtifactManager, RuntimeDebugConfig
from novelty_agent_framework.persistence import ReferenceStore
from novelty_agent_framework.schemas import NoveltyPointReviewRequest
from novelty_agent_framework.tools import ReviewerReaderTool, ResearcherToolRegistry
from novelty_agent_framework.tools.reference_reader import ReferenceArtifactReaderTool

from scripts.run_reviewer_stability_acceptance import EXPERIMENT, LEDGER, ROOT, SharedBudgetClient, save


SOURCE_ROOT = ROOT / "docs/experiments/20260917_005448/runs/full/0002"
SUBJECT = "MG19333vrw-debug-full-20260907"
SOURCE = SOURCE_ROOT / SUBJECT
STAGE = next((SOURCE / "runtime").glob("*/stages/0012_review_evidence/input.json"))
CASES = (("NP-1", "card_249ea45b7685a07e5476a163", "wrk_eba9d160fb1590c9de66938d"),
         ("NP-2", "card_7fe969471654f4f8e4f27f48", "wrk_2e2856efebb6106c4344fc13"))
TRIAL_ROOT = EXPERIMENT / "trials/holdout"


def cases_and_preflight():
    archived = json.loads(STAGE.read_text())
    frozen = json.loads((ROOT / "docs/experiments/20260918_reviewer_grounding_transfer/holdout-manifest.json").read_text())
    requests = []
    reader = ReviewerReaderTool(ReferenceArtifactReaderTool(ReferenceStore(SOURCE_ROOT)))
    for point_id, card_id, work_id in CASES:
        freeze = next(c for c in frozen["cases"] if c["point_id"] == point_id and c["work_id"] == work_id)
        card = next(c for c in archived["validator_accepted_cards"] if c["card_id"] == card_id)
        request = NoveltyPointReviewRequest(
            subject_paper_id=SUBJECT,
            novelty_point=next(p for p in archived["novelty_points"] if p["point_id"] == point_id),
            tasks=[t for t in archived["all_research_tasks"] if t["novelty_point_id"] == point_id],
            cards=[card],
            evidence=[e for e in archived["raw_evidence"] if e["evidence_id"] in card["evidence_ids"]],
        )
        if request.cards[0].evidence_ids != card["evidence_ids"]:
            raise ValueError("holdout evidence selection changed")
        for source in freeze["artifacts"]:
            path = ROOT / source["path"]
            if hashlib.sha256(path.read_bytes()).hexdigest() != source["sha256"]:
                raise ValueError("frozen holdout source text changed")
        catalog = reader.material_catalog(request)
        if not catalog or any(item["work_id"] != work_id for item in catalog):
            raise ValueError("holdout Reader catalog is unavailable or out of scope")
        requests.append((request, catalog))
    preflight = {"source_stage": str(STAGE.relative_to(ROOT)),
        "source_stage_sha256": hashlib.sha256(STAGE.read_bytes()).hexdigest(),
        "holdout_manifest_sha256": hashlib.sha256((ROOT / "docs/experiments/20260918_reviewer_grounding_transfer/holdout-manifest.json").read_bytes()).hexdigest(),
        "cases": [{"point_id": req.novelty_point.point_id, "card_id": req.cards[0].card_id,
                    "request_sha256": hashlib.sha256(req.model_dump_json().encode()).hexdigest(),
                    "material_catalog": catalog} for req, catalog in requests],
        "model": "deepseek-ai/DeepSeek-V4-Flash", "max_output_tokens_per_call": 2048,
        "card_timeout_seconds": 240, "summary_timeout_seconds": 180,
        "max_steps_per_card": 5, "max_tool_calls_per_card": 4,
        "shared_budget_ledger": str(LEDGER.relative_to(ROOT)),
        "new_search_or_download": False,
        "stop_rule": "stop on technical failure, timeout, scope error, or exhausted shared budget"}
    return requests, reader, preflight


async def run(*, live: bool) -> None:
    requests, reader, preflight = cases_and_preflight()
    preflight_path = EXPERIMENT / "fixtures/holdout-preflight.json"
    if preflight_path.exists() and json.loads(preflight_path.read_text()) != preflight:
        raise ValueError("holdout preflight changed after freeze")
    save(preflight_path, preflight)
    if not live:
        return
    if TRIAL_ROOT.exists() and any(TRIAL_ROOT.iterdir()):
        raise FileExistsError("holdout trial already exists; no selective rerun")
    registry = build_model_registry(load_application_config())
    inner = registry.client_for("deepseek-flash")
    if not inner.profile.api_key:
        raise RuntimeError("SILICONFLOW_API_KEY unavailable")
    if not LEDGER.exists():
        raise RuntimeError("shared authorized budget ledger is missing")
    client = SharedBudgetClient(inner)
    reviewer = NoveltyEvidenceReviewer(model_client=client, prompts=build_prompt_library(),
        tool_registry=ResearcherToolRegistry([reader]),
        config=EvidenceReviewerConfig(enabled=True, max_steps=5, max_tool_calls=4,
            max_total_read_chars=8000, card_timeout_seconds=240, summary_timeout_seconds=180,
            summary_input_date="2026-09-17"),
        model_options=ModelCallOptions(temperature=0, max_tokens=2048, timeout_seconds=90,
            tool_choice="auto", extra_body={"enable_thinking": False}))
    statuses = []
    for index, (request, catalog) in enumerate(requests):
        name = f"case-{index + 1}-{request.novelty_point.point_id}"
        output = TRIAL_ROOT / name
        manager = RuntimeArtifactManager(SUBJECT,
            config=RuntimeDebugConfig(output_root=output / "outputs", archive_root=output / "archive",
                max_model_calls=10, max_physical_provider_requests=1),
            run_id=f"rv-stable-holdout-{index + 1}", model_provider="siliconflow",
            model_name=inner.profile.model, enabled_tools=["reader"],
            stage_names=["review_card", "summarize_reviews"], runtime_config=preflight)
        manager.activate()
        try:
            handle = manager.start_stage("review_card", {"point_id": request.novelty_point.point_id,
                "card_id": request.cards[0].card_id, "request": request.model_dump(mode="json")})
            card_review = await reviewer.review_card(request)
            manager.finish_stage(handle, card_review.model_dump(mode="json"))
            save(output / "card-review.json", card_review.model_dump(mode="json"))
            if card_review.incomplete_reason in {"budget_exhausted", "technical_error", "material_unavailable"}:
                status = {"case": name, "execution": "failed_card", "incomplete_reason": card_review.incomplete_reason}
                manager.finish_run("FAILED")
            else:
                row = {"index": 0, "card_id": request.cards[0].card_id,
                       "novelty_point_id": request.novelty_point.point_id,
                       "status": "completed", "review": card_review.model_dump(mode="json")}
                handle = manager.start_stage("summarize_reviews", {"point_id": request.novelty_point.point_id,
                    "card_reviews": [row]})
                summary = await reviewer.summarize_reviews(request, [row])
                manager.finish_stage(handle, summary.model_dump(mode="json"))
                save(output / "summary-review.json", summary.model_dump(mode="json"))
                success = summary.incomplete_reason not in {"budget_exhausted", "technical_error", "material_unavailable"}
                status = {"case": name, "execution": "completed" if success else "failed_summary",
                          "card_status": card_review.status.value,
                          "summary_status": summary.status.value,
                          "incomplete_reason": summary.incomplete_reason}
                manager.finish_run("SUCCESS" if success else "FAILED")
            status["physical_attempts_total"] = len(json.loads(LEDGER.read_text())["attempts"])
            status["completed_at"] = datetime.now(timezone.utc).isoformat()
            save(output / "status.json", status)
            statuses.append(status)
            save(TRIAL_ROOT / "batch-status.json", statuses)
            if status["execution"] != "completed":
                if index + 1 < len(requests):
                    statuses.append({"case": f"case-{index + 2}-{requests[index + 1][0].novelty_point.point_id}",
                                     "execution": "not_run_after_failure"})
                    save(TRIAL_ROOT / "batch-status.json", statuses)
                break
        except BaseException as exc:
            manager.finish_run("FAILED", error=exc)
            raise
        finally:
            manager.deactivate()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true")
    args = parser.parse_args()
    asyncio.run(run(live=args.live))
