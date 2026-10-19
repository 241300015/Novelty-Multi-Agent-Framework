"""Run four pre-registered semantic controls under the shared live budget."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from datetime import datetime, timezone

from backend.env import ModelCallOptions
from novelty_agent_framework.agents.evidence_reviewer import EvidenceReviewerConfig, NoveltyEvidenceReviewer
from novelty_agent_framework.config.factory import build_model_registry, build_prompt_library
from novelty_agent_framework.config.loader import load_application_config
from novelty_agent_framework.core.runtime_artifacts import RuntimeArtifactManager, RuntimeDebugConfig
from novelty_agent_framework.schemas import NoveltyPointReviewRequest

from scripts.run_reviewer_stability_acceptance import EXPERIMENT, LEDGER, ROOT, SharedBudgetClient, save


SOURCE = EXPERIMENT / "fixtures/synthetic-semantic-controls.json"
TRIAL_ROOT = EXPERIMENT / "trials/synthetic-controls"


def build_requests():
    frozen = json.loads(SOURCE.read_text())
    requests = []
    for row in frozen["cases"]:
        case = row["id"]
        point_id, task_id, card_id, evidence_id = (f"{case}-{part}" for part in ("point", "task", "card", "evidence"))
        request = NoveltyPointReviewRequest.model_validate({
            "subject_paper_id": f"synthetic-{case}",
            "novelty_point": {"point_id": point_id, "claim": row["feature"],
                              "technical_features": [row["feature"]]},
            "tasks": [{"task_id": task_id, "novelty_point_id": point_id,
                       "task_type": "semantic_control", "language": "en"}],
            "cards": [{"card_id": card_id, "task_id": task_id, "novelty_point_id": point_id,
                       "document_title": f"Synthetic comparison: {case}",
                       "main_contribution": row["source"], "overlaps": [], "differences": [],
                       "sources": [{"title": f"Synthetic comparison: {case}", "quote": row["source"],
                                    "location": "frozen control text"}],
                       "relevance": 1.0, "confidence": 1.0, "evidence_ids": [evidence_id]}],
            "evidence": [{"evidence_id": evidence_id, "work_id": f"{case}-work",
                          "artifact_id": f"{case}-artifact", "novelty_point_id": point_id,
                          "task_id": task_id, "quote": row["source"],
                          "interpretation": "Frozen synthetic comparison text", "confidence": 1.0}],
        })
        requests.append((case, request))
    return requests


async def run(*, live: bool):
    requests = build_requests()
    preflight = {"source": str(SOURCE.relative_to(ROOT)),
                 "source_sha256": hashlib.sha256(SOURCE.read_bytes()).hexdigest(),
                 "requests": [{"case": case, "request_sha256": hashlib.sha256(
                     request.model_dump_json().encode()).hexdigest()}
                     for case, request in requests],
                 "model": "deepseek-ai/DeepSeek-V4-Flash", "max_output_tokens_per_call": 2048,
                 "card_timeout_seconds": 120, "max_steps_per_card": 2,
                 "max_tool_calls_per_card": 1, "max_total_read_chars": 1,
                 "no_reader_or_search": True,
                 "shared_budget_ledger": str(LEDGER.relative_to(ROOT)),
                 "stop_rule": "stop on technical failure, timeout, or exhausted shared budget"}
    path = EXPERIMENT / "fixtures/synthetic-preflight.json"
    if path.exists() and json.loads(path.read_text()) != preflight:
        raise ValueError("synthetic preflight changed after freeze")
    save(path, preflight)
    if not live:
        return
    if TRIAL_ROOT.exists() and any(TRIAL_ROOT.iterdir()):
        raise FileExistsError("synthetic trials already exist; no selective rerun")
    registry = build_model_registry(load_application_config())
    inner = registry.client_for("deepseek-flash")
    if not inner.profile.api_key or not LEDGER.exists():
        raise RuntimeError("model key or shared authorized budget ledger unavailable")
    reviewer = NoveltyEvidenceReviewer(model_client=SharedBudgetClient(inner),
        prompts=build_prompt_library(),
        config=EvidenceReviewerConfig(enabled=True, max_steps=2, max_tool_calls=1,
            card_timeout_seconds=120, max_total_read_chars=1),
        model_options=ModelCallOptions(temperature=0, max_tokens=2048,
            timeout_seconds=90, tool_choice="none", extra_body={"enable_thinking": False}))
    statuses = []
    for index, (case, request) in enumerate(requests):
        output = TRIAL_ROOT / case
        manager = RuntimeArtifactManager(request.subject_paper_id,
            config=RuntimeDebugConfig(output_root=output / "outputs", archive_root=output / "archive",
                max_model_calls=2, max_physical_provider_requests=1),
            run_id=f"rv-synthetic-{index + 1}", model_provider="siliconflow",
            model_name=inner.profile.model, enabled_tools=[], stage_names=["review_card"],
            runtime_config=preflight)
        manager.activate()
        try:
            handle = manager.start_stage("review_card", {"case": case,
                "request": request.model_dump(mode="json")})
            result = await reviewer.review_card(request)
            save(output / "card-review.json", result.model_dump(mode="json"))
            manager.finish_stage(handle, result.model_dump(mode="json"))
            success = result.incomplete_reason not in {"budget_exhausted", "technical_error", "material_unavailable"}
            status = {"case": case, "execution": "completed" if success else "failed",
                      "review_status": result.status.value,
                      "incomplete_reason": result.incomplete_reason,
                      "physical_attempts_total": len(json.loads(LEDGER.read_text())["attempts"]),
                      "completed_at": datetime.now(timezone.utc).isoformat()}
            manager.finish_run("SUCCESS" if success else "FAILED")
            save(output / "status.json", status)
            statuses.append(status)
            save(TRIAL_ROOT / "batch-status.json", statuses)
            if not success:
                statuses.extend({"case": later, "execution": "not_run_after_failure"}
                                for later, _ in requests[index + 1:])
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
    asyncio.run(run(live=parser.parse_args().live))
