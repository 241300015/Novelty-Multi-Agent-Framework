"""Run the fixed NP-3 Reviewer-only L0/L1 pair from the archived review input."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from dataclasses import dataclass, field

from backend.env import ModelCallBudgetExceeded, ModelCallOptions
from novelty_agent_framework.agents.evidence_reviewer import EvidenceReviewerConfig, NoveltyEvidenceReviewer
from novelty_agent_framework.config.factory import build_model_registry, build_prompt_library
from novelty_agent_framework.config.loader import load_application_config
from novelty_agent_framework.core.runtime_artifacts import RuntimeArtifactManager, RuntimeDebugConfig
from novelty_agent_framework.persistence import ReferenceStore
from novelty_agent_framework.schemas import EvidenceCard, Evidence, NoveltyPoint, NoveltyPointReviewRequest, ResearchTask
from novelty_agent_framework.schemas import ReviewerCardDraft, ReviewerSummaryDraft
from novelty_agent_framework.tools.reader import ReviewerReaderTool
from novelty_agent_framework.tools.reference_reader import ReferenceArtifactReaderTool
from novelty_agent_framework.tools.researcher_registry import ResearcherToolRegistry


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "docs/experiments/20260917_214820_retrieval_repair/runs/full/0001/MF2033k6lC"
STAGE_INPUT = SOURCE / "runtime/run-d606cd37534a47349b905d42c6141768/stages/0012_review_evidence/input.json"
EXPERIMENT = ROOT / "docs/experiments/20260918_000421_reviewer_evidence_repair"
PAPER = "MF2033k6lC"
CARD_IDS = ("card_055dcd710a45194158c1d5ba", "card_927caf724042beff967b2231")
ABSTRACT_IDS = {"art_082358a7cb0efe62d471f7fb", "art_044b624240993e16c40f3537"}
MODEL_CALL_LIMIT = 24
MAX_REQUEST_BYTES = 60_000
MAX_OUTPUT_TOKENS = 2048
COST_CAP_RMB = 1.00
INPUT_RATE = 3.0 / 1_000_000
CACHED_INPUT_RATE = 0.3 / 1_000_000
OUTPUT_RATE = 9.0 / 1_000_000


def save(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


class AbstractReader(ReviewerReaderTool):
    def material_catalog(self, scope):
        return [item for item in super().material_catalog(scope) if item["artifact_id"] in ABSTRACT_IDS]


class BudgetedClient:
    def __init__(self, inner, ledger):
        self.inner, self.ledger = inner, ledger

    async def acomplete(self, messages, *, options=None):
        options = options or ModelCallOptions()
        payload = self.inner._build_payload(messages, options)
        size = len(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
        if size > MAX_REQUEST_BYTES:
            raise ModelCallBudgetExceeded(f"model input exceeds frozen byte cap: {size}")
        tokens = options.max_tokens or MAX_OUTPUT_TOKENS
        if tokens > MAX_OUTPUT_TOKENS:
            raise ModelCallBudgetExceeded("model output token cap exceeded")
        bound = size * INPUT_RATE + tokens * OUTPUT_RATE
        if len(self.ledger["calls"]) >= MODEL_CALL_LIMIT or self.ledger["spent_rmb"] + bound > COST_CAP_RMB:
            raise ModelCallBudgetExceeded("frozen Reviewer-only budget exhausted")
        call = {"request_bytes": size, "max_output_tokens": tokens, "worst_case_rmb": round(bound, 6), "started_at": datetime.now(timezone.utc).isoformat()}
        self.ledger["calls"].append(call)
        self.ledger["spent_rmb"] += bound
        try:
            response = await self.inner.acomplete(messages, options=options)
            usage = dict(response.usage)
            call["usage"] = usage
            input_tokens = usage.get("prompt_tokens")
            output_tokens = usage.get("completion_tokens")
            if isinstance(input_tokens, int) and isinstance(output_tokens, int):
                cached = usage.get("prompt_cache_hit_tokens", 0)
                actual = (input_tokens - cached) * INPUT_RATE + cached * CACHED_INPUT_RATE + output_tokens * OUTPUT_RATE
                call["estimated_cost_rmb"] = round(actual, 7)
                self.ledger["spent_rmb"] += actual - bound
            call["completed_at"] = datetime.now(timezone.utc).isoformat()
            return response
        except Exception as exc:
            call["error"] = f"{type(exc).__name__}: {exc}"[:300]
            raise


def build_request(raw):
    point = NoveltyPoint.model_validate(next(p for p in raw["novelty_points"] if p["point_id"] == "NP-3"))
    cards = [EvidenceCard.model_validate(c) for c in raw["validator_accepted_cards"] if c["card_id"] in CARD_IDS]
    assert {c.card_id for c in cards} == set(CARD_IDS)
    cited = {e for card in cards for e in card.evidence_ids}
    evidence = [Evidence.model_validate(e) for e in raw["raw_evidence"] if e["evidence_id"] in cited]
    assert {e.evidence_id for e in evidence} == cited
    tasks = [ResearchTask.model_validate(t) for t in raw["all_research_tasks"] if t["novelty_point_id"] == "NP-3"]
    return NoveltyPointReviewRequest(subject_paper_id=PAPER, novelty_point=point, tasks=tasks, cards=cards, evidence=evidence)


def preflight_materials(request, prompts):
    reader = ReviewerReaderTool(ReferenceArtifactReaderTool(
        ReferenceStore(output_root=SOURCE.parent)))
    reviewer = NoveltyEvidenceReviewer(model_client=object(), prompts=prompts,
        tool_registry=ResearcherToolRegistry([reader]))
    expected = {
        CARD_IDS[0]: {"art_082358a7cb0efe62d471f7fb", "art_ebc47b0e70f5b363f3d871f2"},
        CARD_IDS[1]: {"art_044b624240993e16c40f3537", "art_239d9f203a9c73612f1cbbaa"},
    }
    rows = []
    for card in request.cards:
        single = NoveltyPointReviewRequest(subject_paper_id=PAPER,
            novelty_point=request.novelty_point, tasks=request.tasks, cards=[card],
            evidence=[e for e in request.evidence if e.evidence_id in card.evidence_ids])
        catalog = reader.material_catalog(single)
        if {item["artifact_id"] for item in catalog} != expected[card.card_id]:
            raise ValueError(f"material identity mismatch for {card.card_id}")
        system, user = reviewer._render_point_prompt(single)
        rows.append({"card_id": card.card_id, "catalog": catalog,
            "prompt_sha256": hashlib.sha256((system + "\n" + user).encode()).hexdigest(),
            "card_draft_schema_sha256": hashlib.sha256(json.dumps(
                ReviewerCardDraft.model_json_schema(), sort_keys=True).encode()).hexdigest()})
    return rows


@dataclass
class VariantSession:
    variant: str
    reviewer: NoveltyEvidenceReviewer
    manager: RuntimeArtifactManager
    rows: list[dict] = field(default_factory=list)


def prepare_variant(variant, registry, prompts, ledger, trial_root):
    output = trial_root / variant / "outputs"
    destination = output / PAPER / "references"
    shutil.copytree(SOURCE / "references", destination)
    reader_cls = AbstractReader if variant == "L0" else ReviewerReaderTool
    reader = reader_cls(ReferenceArtifactReaderTool(ReferenceStore(output_root=output)))
    model = BudgetedClient(registry.client_for("deepseek-flash"), ledger)
    reviewer = NoveltyEvidenceReviewer(
        model_client=model, prompts=prompts, tool_registry=ResearcherToolRegistry([reader]),
        config=EvidenceReviewerConfig(enabled=True, max_steps=5, max_tool_calls=4,
                                      max_total_read_chars=8000, card_timeout_seconds=240,
                                      summary_timeout_seconds=180),
        model_options=ModelCallOptions(temperature=0, max_tokens=MAX_OUTPUT_TOKENS,
                                       timeout_seconds=90, tool_choice="auto",
                                       extra_body={"enable_thinking": False}),
    )
    manager = RuntimeArtifactManager(PAPER, config=RuntimeDebugConfig(
        output_root=output, archive_root=trial_root / variant / "archive",
        max_model_calls=13, max_physical_provider_requests=1),
        run_id=f"rv-np3-{variant.lower()}", model_provider="siliconflow",
        model_name="deepseek-ai/DeepSeek-V4-Flash", enabled_tools=["reader"],
        stage_names=["review_card", "summarize_reviews"],
        runtime_config={"variant": variant, "max_steps": 5, "max_tool_calls": 4,
                        "max_total_read_chars_per_card": 8000, "max_output_tokens": MAX_OUTPUT_TOKENS,
                        "cost_cap_rmb_pair": COST_CAP_RMB})
    return VariantSession(variant, reviewer, manager)


async def run_card(session, request, card, index, trial_root):
    session.manager.activate()
    try:
        single = NoveltyPointReviewRequest(
            subject_paper_id=PAPER, novelty_point=request.novelty_point,
            tasks=request.tasks, cards=[card],
            evidence=[e for e in request.evidence if e.evidence_id in card.evidence_ids])
        handle = session.manager.start_stage("review_card", {"point_id": "NP-3", "card_id": card.card_id,
            "variant_id": session.variant, "request": single.model_dump(mode="json")})
        review = await session.reviewer.review_card(single)
        row = {"index": index, "card_id": card.card_id, "novelty_point_id": "NP-3",
               "status": "completed", "review": review.model_dump(mode="json")}
        session.rows.append(row)
        session.manager.finish_stage(handle, row)
        save(trial_root / session.variant / f"card-{index + 1}.json", row)
        return row
    except BaseException as exc:
        session.manager.finish_run("FAILED", error=exc)
        raise
    finally:
        session.manager.deactivate()


async def finish_variant(session, request, trial_root, *, skip_model=False):
    session.manager.activate()
    try:
        if skip_model:
            status = {"status": "not_run", "reason": "protocol_or_technical_error_in_first_pair"}
            save(trial_root / session.variant / "summary-status.json", status)
            session.manager.finish_run("FAILED")
            return status
        handle = session.manager.start_stage("summarize_reviews", {"point_id": "NP-3",
            "variant_id": session.variant, "card_reviews": session.rows})
        result = await session.reviewer.summarize_reviews(request, session.rows)
        session.manager.finish_stage(handle, result.model_dump(mode="json"))
        save(trial_root / session.variant / "summary-review.json", result.model_dump(mode="json"))
        session.manager.finish_run("SUCCESS")
        return result.model_dump(mode="json")
    except BaseException as exc:
        session.manager.finish_run("FAILED", error=exc)
        raise
    finally:
        session.manager.deactivate()


async def main(trial_root, *, live: bool):
    if trial_root.exists():
        raise FileExistsError(f"trial already exists; refusing to reset its budget: {trial_root}")
    raw = json.loads(STAGE_INPUT.read_text(encoding="utf-8"))
    request = build_request(raw)
    cfg = load_application_config()
    registry = build_model_registry(cfg)
    client = registry.client_for("deepseek-flash")
    prompts = build_prompt_library()
    material_preflight = preflight_materials(request, prompts)
    ledger = {"calls": [], "spent_rmb": 0.0, "cap_rmb": COST_CAP_RMB}
    save(trial_root / "frozen-input.json", request.model_dump(mode="json"))
    save(trial_root / "preflight.json", {
        "source_stage_input": str(STAGE_INPUT.relative_to(ROOT)),
        "source_sha256": hashlib.sha256(STAGE_INPUT.read_bytes()).hexdigest(),
        "variants": ["L0_prime", "L1_prime"], "model": client.profile.model,
        "destination": client.profile.base_url, "live_authorized": live,
        "card_schema": ReviewerCardDraft.model_json_schema(),
        "summary_schema": ReviewerSummaryDraft.model_json_schema(),
        "material_preflight": material_preflight,
        "model_call_limit_pair": MODEL_CALL_LIMIT, "max_request_bytes": MAX_REQUEST_BYTES,
        "max_output_tokens_per_call": MAX_OUTPUT_TOKENS, "max_tool_calls_per_card": 4,
        "max_read_chars_per_card": 8000, "card_timeout_seconds": 240,
        "summary_timeout_seconds": 180, "format_retries": "at most one per card; no recursive repair",
        "cost_cap_rmb_pair": COST_CAP_RMB,
    })
    if not live:
        return
    if not client.profile.api_key:
        raise RuntimeError("SILICONFLOW_API_KEY unavailable")
    sessions = {v: prepare_variant(v, registry, prompts, ledger, trial_root)
                for v in ("L0", "L1")}
    stop_for_protocol = False
    try:
        for index, card in enumerate(request.cards):
            pair = [await run_card(sessions[variant], request, card, index, trial_root)
                    for variant in ("L0", "L1")]
            save(trial_root / f"card-pair-{index + 1}.json", pair)
            save(trial_root / "budget-ledger.json", ledger)
            if any(row["review"].get("incomplete_reason") == "technical_error" for row in pair):
                stop_for_protocol = True
                save(trial_root / "stop.json", {"reason": "protocol_or_technical_error_in_pair",
                    "after_card_pair": index + 1})
                break
        for variant in ("L0", "L1"):
            result = await finish_variant(sessions[variant], request, trial_root,
                                          skip_model=stop_for_protocol)
            save(trial_root / f"{variant}-result.json", result)
            save(trial_root / "budget-ledger.json", ledger)
    finally:
        save(trial_root / "budget-ledger.json", ledger)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("trial_root", type=Path, help="New isolated trial directory; existing paths are rejected")
    parser.add_argument("--live", action="store_true", help="Send fixed NP-3 material to SiliconFlow after separate approval")
    args = parser.parse_args()
    asyncio.run(main(args.trial_root.resolve(), live=args.live))
