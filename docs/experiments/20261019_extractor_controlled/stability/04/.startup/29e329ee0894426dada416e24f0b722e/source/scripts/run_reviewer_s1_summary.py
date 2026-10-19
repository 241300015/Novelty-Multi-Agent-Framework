"""Run the frozen NP-3 S1 summary only, with an independent two-call budget."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from backend.env import ModelCallBudgetExceeded, ModelCallOptions
from novelty_agent_framework.agents.evidence_reviewer import (
    EvidenceReviewerConfig, NoveltyEvidenceReviewer, _compact_summary_rows,
    _summary_model_rows, _verify_summary_payload,
)
from novelty_agent_framework.config.factory import build_model_registry, build_prompt_library
from novelty_agent_framework.config.loader import load_application_config
from novelty_agent_framework.core.runtime_artifacts import RuntimeArtifactManager, RuntimeDebugConfig
from novelty_agent_framework.schemas import NoveltyPointReviewRequest


ROOT = Path(__file__).resolve().parents[1]
ARCHIVE = ROOT / "docs/experiments/20260918_reviewer_contract_finalize/trials/np3_postrepair_pair_20260918"
EVENT = ARCHIVE / "L1/outputs/MF2033k6lC/runtime/rv-np3-l1/reviewer_events/0005.json"
OUTPUT = ROOT / "docs/experiments/20260918_reviewer_grounding_transfer/trials/s1_summary_live"
MAX_CALLS, MAX_OUTPUT_TOKENS, MAX_REQUEST_BYTES = 2, 2048, 60_000
COST_CAP_RMB = 0.40
INPUT_RATE, CACHED_INPUT_RATE, OUTPUT_RATE = 3 / 1_000_000, 0.3 / 1_000_000, 9 / 1_000_000


def save(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def summary_execution_status(review) -> str:
    return ("FAILED" if review.incomplete_reason in
            {"technical_error", "budget_exhausted", "material_unavailable"} else "SUCCESS")


class BudgetedClient:
    def __init__(self, inner, ledger_path: Path):
        self.inner, self.ledger_path = inner, ledger_path
        self.ledger = {"calls": [], "estimated_spent_rmb": 0.0,
                       "cap_rmb": COST_CAP_RMB, "max_calls": MAX_CALLS}

    async def acomplete(self, messages, *, options=None):
        options = options or ModelCallOptions()
        payload = self.inner._build_payload(messages, options)
        size = len(json.dumps(payload, ensure_ascii=False).encode())
        tokens = options.max_tokens or MAX_OUTPUT_TOKENS
        bound = size * INPUT_RATE + tokens * OUTPUT_RATE
        if size > MAX_REQUEST_BYTES or tokens > MAX_OUTPUT_TOKENS:
            raise ModelCallBudgetExceeded("S1 request or output limit exceeded")
        if len(self.ledger["calls"]) >= MAX_CALLS or self.ledger["estimated_spent_rmb"] + bound > COST_CAP_RMB:
            raise ModelCallBudgetExceeded("S1 call or cost cap exhausted")
        call = {"request_bytes": size, "max_output_tokens": tokens,
                "reserved_rmb": round(bound, 7), "started_at": datetime.now(timezone.utc).isoformat()}
        self.ledger["calls"].append(call)
        self.ledger["estimated_spent_rmb"] += bound
        save(self.ledger_path, self.ledger)
        try:
            response = await self.inner.acomplete(messages, options=options)
            usage = dict(response.usage)
            call["usage"] = usage
            input_tokens, output_tokens = usage.get("prompt_tokens"), usage.get("completion_tokens")
            if isinstance(input_tokens, int) and isinstance(output_tokens, int):
                cached = usage.get("prompt_cache_hit_tokens", 0)
                actual = (input_tokens - cached) * INPUT_RATE + cached * CACHED_INPUT_RATE + output_tokens * OUTPUT_RATE
                call["estimated_cost_rmb"] = round(actual, 7)
                self.ledger["estimated_spent_rmb"] += actual - bound
            call["completed_at"] = datetime.now(timezone.utc).isoformat()
            return response
        except BaseException as exc:
            call["error"] = f"{type(exc).__name__}: {exc}"[:300]
            raise
        finally:
            save(self.ledger_path, self.ledger)


async def run(*, live: bool) -> None:
    if OUTPUT.exists() and (not live or (OUTPUT / "budget-ledger.json").exists()
                            or (OUTPUT / "summary-review.json").exists()):
        raise FileExistsError(f"S1 trial already exists; budget cannot be reset: {OUTPUT}")
    raw_request = ARCHIVE / "frozen-input.json"
    request = NoveltyPointReviewRequest.model_validate_json(raw_request.read_text())
    event = json.loads(EVENT.read_text())
    card_reviews = [{key: row[key] for key in ("index", "card_id", "novelty_point_id", "status", "review")}
                    for row in event["rows"]]
    rows, _ = _compact_summary_rows(request, card_reviews)
    projected = _summary_model_rows(rows)
    payload = {"today": datetime.now(timezone.utc).date().isoformat(),
               "novelty_point": request.novelty_point.model_dump(mode="json"),
               "card_reviews": projected}
    _verify_summary_payload(rows, json.dumps(payload, ensure_ascii=False))
    expected = json.loads((OUTPUT.parent.parent / "s1-prepared-payload.json").read_text())["payload"]
    if payload["novelty_point"] != expected["novelty_point"] or payload["card_reviews"] != expected["card_reviews"]:
        raise ValueError("frozen S1 rows differ from offline prepared payload")
    registry = build_model_registry(load_application_config())
    inner = registry.client_for("deepseek-flash")
    prompts = build_prompt_library()
    prompt_path = ROOT / "backend/src/novelty_agent_framework/prompts/reviewer/summarize_reviews.md"
    preflight = {"hypothesis": "selected registered read quotes reach actual S1 summary request",
        "source_input_sha256": sha256(raw_request.read_bytes()), "source_event_sha256": sha256(EVENT.read_bytes()),
        "summary_prompt_sha256": sha256(prompt_path.read_bytes()), "model": inner.profile.model,
        "destination": inner.profile.base_url, "live_authorized": live,
        "max_calls_including_repair": MAX_CALLS, "max_output_tokens_per_call": MAX_OUTPUT_TOKENS,
        "max_request_bytes": MAX_REQUEST_BYTES, "cost_cap_rmb": COST_CAP_RMB,
        "timeout_seconds": 180, "stop_after": "one S1 summary and at most one format repair",
        "card_count": len(card_reviews), "quote_count": sum(len(r.get("key_quotes", [])) for r in projected)}
    prior = OUTPUT / "preflight.json"
    if live and prior.exists():
        previous = json.loads(prior.read_text())
        if {key: value for key, value in previous.items() if key != "live_authorized"} != {
                key: value for key, value in preflight.items() if key != "live_authorized"}:
            raise ValueError("S1 preflight changed after freeze")
    save(OUTPUT / "preflight.json", preflight)
    save(OUTPUT / "frozen-card-reviews.json", card_reviews)
    if not live:
        return
    if not inner.profile.api_key:
        raise RuntimeError("SILICONFLOW_API_KEY unavailable")
    client = BudgetedClient(inner, OUTPUT / "budget-ledger.json")
    reviewer = NoveltyEvidenceReviewer(model_client=client, prompts=prompts,
        config=EvidenceReviewerConfig(enabled=True, summary_timeout_seconds=180,
                                      summary_input_date=expected["today"]),
        model_options=ModelCallOptions(temperature=0, max_tokens=MAX_OUTPUT_TOKENS,
            timeout_seconds=90, tool_choice="none", extra_body={"enable_thinking": False}))
    manager = RuntimeArtifactManager(request.subject_paper_id,
        config=RuntimeDebugConfig(output_root=OUTPUT / "outputs", archive_root=OUTPUT / "archive",
            max_model_calls=MAX_CALLS, max_physical_provider_requests=1),
        run_id="rv-ground-s1-summary", model_provider="siliconflow",
        model_name=inner.profile.model, enabled_tools=[], stage_names=["summarize_reviews"],
        runtime_config=preflight)
    manager.activate()
    try:
        handle = manager.start_stage("summarize_reviews", {"point_id": request.novelty_point.point_id,
            "variant_id": "S1", "card_reviews": card_reviews})
        result = await reviewer.summarize_reviews(request, card_reviews)
        save(OUTPUT / "summary-review.json", result.model_dump(mode="json"))
        manager.finish_stage(handle, result.model_dump(mode="json"))
        manager.finish_run(summary_execution_status(result))
    except BaseException as exc:
        manager.finish_run("FAILED", error=exc)
        raise
    finally:
        manager.deactivate()
        save(OUTPUT / "budget-ledger.json", client.ledger)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true")
    arguments = parser.parse_args()
    asyncio.run(run(live=arguments.live))
