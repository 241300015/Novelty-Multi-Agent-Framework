"""Frozen Reviewer-only stability batch; no calls unless --live is supplied."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from backend.env import ChatMessage, ModelCallBudgetExceeded, ModelCallOptions
from novelty_agent_framework.agents.evidence_reviewer import (
    EvidenceReviewerConfig, NoveltyEvidenceReviewer, _EVIDENCE_BOUNDARY,
    _SUMMARY_FALLBACK, _compact_summary_rows, _summary_model_rows,
)
from novelty_agent_framework.config.factory import build_model_registry, build_prompt_library
from novelty_agent_framework.config.loader import load_application_config
from novelty_agent_framework.core.runtime_artifacts import RuntimeArtifactManager, RuntimeDebugConfig
from novelty_agent_framework.schemas import NoveltyPointReviewRequest, ReviewerSummaryDraft


ROOT = Path(__file__).resolve().parents[1]
EXPERIMENT = ROOT / "docs/experiments/20260918_reviewer_stability_acceptance"
FROZEN = ROOT / "docs/experiments/20260918_reviewer_contract_finalize/trials/np3_postrepair_pair_20260918"
HISTORICAL = ROOT / "docs/experiments/20260918_reviewer_grounding_transfer/trials/s1_summary_live/outputs/MF2033k6lC/runtime/rv-ground-s1-summary/llm_calls/0001_deepseek-flash.json"
TRIAL_ROOT = EXPERIMENT / "trials/summary-repeats"
LEDGER = EXPERIMENT / "analysis/live-budget-ledger.json"
MAX_CALLS, MAX_OUTPUT_TOKENS, MAX_REQUEST_BYTES = 24, 2048, 60_000
COST_CAP_RMB = 3.00
INPUT_RATE, CACHED_INPUT_RATE, OUTPUT_RATE = 3 / 1_000_000, 0.3 / 1_000_000, 9 / 1_000_000


def save(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def frozen_material():
    request = NoveltyPointReviewRequest.model_validate_json((FROZEN / "frozen-input.json").read_text())
    event = json.loads((FROZEN / "L1/outputs/MF2033k6lC/runtime/rv-np3-l1/reviewer_events/0005.json").read_text())
    rows = [{key: row[key] for key in ("index", "card_id", "novelty_point_id", "status", "review")}
            for row in event["rows"]]
    return request, rows


class SharedBudgetClient:
    def __init__(self, inner):
        self.inner = inner

    async def acomplete(self, messages, *, options=None):
        options = options or ModelCallOptions()
        payload = self.inner._build_payload(messages, options)
        size = len(json.dumps(payload, ensure_ascii=False).encode())
        tokens = options.max_tokens or MAX_OUTPUT_TOKENS
        reserve = size * INPUT_RATE + tokens * OUTPUT_RATE
        if size > MAX_REQUEST_BYTES or tokens > MAX_OUTPUT_TOKENS:
            raise ModelCallBudgetExceeded("stability request size or output cap exceeded")
        ledger = json.loads(LEDGER.read_text())
        if len(ledger["attempts"]) >= MAX_CALLS or ledger["reserved_total_rmb"] + reserve > COST_CAP_RMB:
            raise ModelCallBudgetExceeded("unified Reviewer stability budget exhausted")
        entry = {"attempt": len(ledger["attempts"]) + 1,
                 "started_at": datetime.now(timezone.utc).isoformat(),
                 "request_bytes": size, "max_output_tokens": tokens,
                 "reserved_rmb": round(reserve, 7),
                 "request_payload_sha256": hashlib.sha256(json.dumps(payload, ensure_ascii=False).encode()).hexdigest()}
        ledger["attempts"].append(entry)
        ledger["reserved_total_rmb"] += reserve
        save(LEDGER, ledger)  # Reserve before entering the provider client.
        try:
            response = await self.inner.acomplete(messages, options=options)
            usage = dict(response.usage)
            entry["usage"] = usage
            prompt, completion = usage.get("prompt_tokens"), usage.get("completion_tokens")
            if isinstance(prompt, int) and isinstance(completion, int):
                cached = usage.get("prompt_cache_hit_tokens", 0)
                entry["estimated_charge_rmb"] = round(
                    (prompt - cached) * INPUT_RATE + cached * CACHED_INPUT_RATE
                    + completion * OUTPUT_RATE, 7)
                entry["billing_status"] = "estimated_from_usage"
            else:
                entry["billing_status"] = "unknown"
            entry["completed_at"] = datetime.now(timezone.utc).isoformat()
            return response
        except BaseException as exc:
            entry["error"] = f"{type(exc).__name__}: {exc}"[:300]
            entry["billing_status"] = "unknown"
            raise
        finally:
            save(LEDGER, ledger)


def preflight(registry, prompts, request, rows):
    client = registry.client_for("deepseek-flash")
    reviewer = NoveltyEvidenceReviewer(model_client=client, prompts=prompts)
    system = reviewer._render_instruction("reviewer/summarize_reviews", _SUMMARY_FALLBACK) + "\n" + _EVIDENCE_BOUNDARY
    compact, _ = _compact_summary_rows(request, rows)
    old_payload = json.loads(HISTORICAL.read_text())["request_payload"]
    old_user = json.loads(old_payload["messages"][1]["content"])
    user = json.dumps({"today": old_user["today"],
        "novelty_point": request.novelty_point.model_dump(mode="json"),
        "card_reviews": _summary_model_rows(compact),
        "review_schema": ReviewerSummaryDraft.model_json_schema()}, ensure_ascii=False)
    options = ModelCallOptions(temperature=0, max_tokens=MAX_OUTPUT_TOKENS,
                               tool_choice="none", extra_body={"enable_thinking": False})
    payload = client._build_payload([ChatMessage(role="system", content=system),
                                     ChatMessage(role="user", content=user)], options)
    if payload != old_payload:
        raise ValueError("frozen S1 model payload changed; repetition is not comparable")
    return {"source": str(HISTORICAL.relative_to(ROOT)),
            "payload_sha256": hashlib.sha256(json.dumps(payload, ensure_ascii=False).encode()).hexdigest(),
            "system_sha256": hashlib.sha256(system.encode()).hexdigest(),
            "user_sha256": hashlib.sha256(user.encode()).hexdigest(),
            "fixed_business_date": old_user["today"], "model": client.profile.model,
            "max_physical_calls": MAX_CALLS, "estimated_cost_cap_rmb": COST_CAP_RMB,
            "max_request_bytes": MAX_REQUEST_BYTES, "max_output_tokens_per_call": MAX_OUTPUT_TOKENS,
            "summary_deadline_seconds": 180,
            "run_ids": [f"S-repeat-{i}" for i in range(1, 4)],
            "stop_rule": "stop after any unexplained timeout or unrecoverable protocol failure"}


async def run(*, live: bool, batch_id: str = "summary-repeats",
              resume_budget: bool = False) -> None:
    request, rows = frozen_material()
    registry = build_model_registry(load_application_config())
    prompts = build_prompt_library()
    prepared = preflight(registry, prompts, request, rows)
    preflight_path = EXPERIMENT / "fixtures/repeat-preflight.json"
    if preflight_path.exists() and json.loads(preflight_path.read_text()) != prepared:
        raise ValueError("repeat preflight changed after freeze")
    save(preflight_path, prepared)
    if not live:
        return
    if batch_id not in {"summary-repeats", "summary-repeats-after-network-gate"}:
        raise ValueError("batch_id is not pre-registered")
    trial_root = EXPERIMENT / "trials" / batch_id
    if not registry.client_for("deepseek-flash").profile.api_key:
        raise RuntimeError("SILICONFLOW_API_KEY unavailable")
    if LEDGER.exists() and json.loads(LEDGER.read_text()).get("attempts") and not resume_budget:
        raise FileExistsError("shared live ledger already contains attempts; use explicit budget resume")
    if resume_budget:
        previous = json.loads(LEDGER.read_text())
        if batch_id != "summary-repeats-after-network-gate" or not previous.get("attempts") \
                or "Operation not permitted" not in previous["attempts"][-1].get("error", ""):
            raise ValueError("budget resume requires the preserved network-denied attempt")
    if trial_root.exists() and any(trial_root.iterdir()):
        raise FileExistsError("repeat trials already exist; no selective rerun")
    if not LEDGER.exists():
        save(LEDGER, {"cap_rmb": COST_CAP_RMB, "max_physical_calls": MAX_CALLS,
                      "reserved_total_rmb": 0.0, "attempts": []})
    client = SharedBudgetClient(registry.client_for("deepseek-flash"))
    reviewer = NoveltyEvidenceReviewer(model_client=client, prompts=prompts,
        config=EvidenceReviewerConfig(enabled=True, summary_timeout_seconds=180,
                                      summary_input_date=prepared["fixed_business_date"]),
        model_options=ModelCallOptions(temperature=0, max_tokens=MAX_OUTPUT_TOKENS,
            timeout_seconds=90, tool_choice="none", extra_body={"enable_thinking": False}))
    statuses = []
    for index in range(1, 4):
        name = f"S-repeat-{index}"
        output = trial_root / name
        manager = RuntimeArtifactManager(request.subject_paper_id,
            config=RuntimeDebugConfig(output_root=output / "outputs", archive_root=output / "archive",
                max_model_calls=2, max_physical_provider_requests=1),
            run_id=f"rv-stable-{index}", model_provider="siliconflow",
            model_name=registry.client_for("deepseek-flash").profile.model,
            enabled_tools=[], stage_names=["summarize_reviews"], runtime_config=prepared)
        manager.activate()
        try:
            handle = manager.start_stage("summarize_reviews", {"point_id": request.novelty_point.point_id,
                "variant_id": name, "card_reviews": rows})
            result = await reviewer.summarize_reviews(request, rows)
            save(output / "summary-review.json", result.model_dump(mode="json"))
            manager.finish_stage(handle, result.model_dump(mode="json"))
            success = result.incomplete_reason not in {"technical_error", "budget_exhausted", "material_unavailable"}
            manager.finish_run("SUCCESS" if success else "FAILED")
            status = {"run_id": name, "execution": "completed" if success else "failed",
                      "review_status": result.status.value, "incomplete_reason": result.incomplete_reason,
                      "physical_attempts_total": len(json.loads(LEDGER.read_text())["attempts"])}
            save(output / "status.json", status)
            statuses.append(status)
            save(trial_root / "batch-status.json", statuses)
            if not success:
                statuses.extend({"run_id": f"S-repeat-{later}", "execution": "not_run_after_failure"}
                                for later in range(index + 1, 4))
                save(trial_root / "batch-status.json", statuses)
                break
        except BaseException as exc:
            manager.finish_run("FAILED", error=exc)
            raise
        finally:
            manager.deactivate()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--batch-id", default="summary-repeats")
    parser.add_argument("--resume-budget", action="store_true")
    args = parser.parse_args()
    asyncio.run(run(live=args.live, batch_id=args.batch_id, resume_budget=args.resume_budget))
