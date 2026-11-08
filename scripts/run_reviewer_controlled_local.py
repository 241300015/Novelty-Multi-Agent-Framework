"""Bounded, explicitly invoked local-model acceptance; no cloud fallback.

Run from the repository root with PYTHONPATH=backend/src:. and --live.
Fixtures are synthetic, saved through the production ReferenceStore and Reader.
Raw inputs/responses and usage remain in a fresh output directory.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import time
from contextvars import ContextVar
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from backend.env import (
    ModelCallBudgetExceeded, ModelCallOptions, ModelProfile, OpenAICompatibleChatClient, PromptLibrary,
)
from novelty_agent_framework.agents.evidence_reviewer import EvidenceReviewerConfig, NoveltyEvidenceReviewer
from novelty_agent_framework.core.integrity_gates import _validate_partial_review_reads
from novelty_agent_framework.persistence import ReferenceStore
from novelty_agent_framework.schemas import (
    Artifact, Evidence, EvidenceCard, EvidenceLocator, EvidenceSource, NoveltyPoint, NoveltyPointReviewRequest,
    ReferenceManifest, ResearchTask, SourceRecord, Work,
)
from novelty_agent_framework.tools import ReferenceArtifactReaderTool, ResearcherToolRegistry, ReviewerReaderTool


ROOT = Path(__file__).resolve().parents[1]
CASE = ContextVar("reviewer_local_case", default="unscoped")
ABSTRACT = "We introduce Lumen, a graph-based anomaly detection method."
BODY = (
    "Methods. Lumen computes the median of each node's neighboring measurements. "
    "It flags a node when its deviation from that median exceeds a fixed threshold of 2.0. "
    "The threshold is manually specified and is not learned from training data. "
    "Evaluation. We report accuracy on two graph datasets."
)
FEATURES = ["使用邻居测量值的中位数进行聚合", "异常检测阈值通过训练学习", "采用差分隐私机制"]


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def fixture(store, name, *, complete=False, missing=False):
    """A real local manifest, original Evidence locator, and optional missing body."""
    now = datetime.now(timezone.utc)
    paper, work, source = "local-" + name, "work-" + name, "source-" + name
    texts = [("abstract-" + name, "abstract", BODY if complete else ABSTRACT),
             ("body-" + name, "extracted_text", BODY)]
    artifacts, paths = [], []
    for artifact, role, text in texts:
        paths.append(store.write_document(paper, work_id=work, artifact_id=artifact, extension="txt", content=text))
        artifacts.append(Artifact(artifact_id=artifact, work_id=work, source_record_id=source,
            role=role, media_type="text/plain", relative_path=f"documents/{work}/{artifact}.txt",
            sha256=hashlib.sha256(text.encode()).hexdigest(), content_extent="full", acquired_at=now))
    store.persist_manifest(paper, ReferenceManifest(subject_paper_id=paper, updated_at=now,
        works=[Work(work_id=work, work_type="article", title="Synthetic Lumen method")],
        source_records=[SourceRecord(source_record_id=source, work_id=work, source_id="local-fixture",
            source_kind="local", title="Synthetic Lumen method", access_status="full_text_acquired", observed_at=now)],
        artifacts=artifacts))
    if missing:
        paths[1].unlink()  # Explicit local fixture fault; does not touch user materials.
    quote = texts[0][2]
    original_read = store.read_document_slice(paper, artifact_id=texts[0][0], char_start=0, max_chars=len(quote))
    point = NoveltyPoint(point_id="NP-1", claim="查证中位数聚合、训练阈值与差分隐私的组合", technical_features=FEATURES)
    task = ResearchTask(task_id="T-1", novelty_point_id=point.point_id, task_type="prior-art", language="en",
        description="逐项核验 F1–F3；摘要不足时优先回读 Methods，不把未知当作已确认差异。")
    evidence = Evidence(evidence_id="E-" + name, work_id=work, artifact_id=texts[0][0],
        novelty_point_id=point.point_id, task_id=task.task_id, quote=quote,
        locator=EvidenceLocator(char_start=0, char_end=len(quote)), interpretation="材料范围以原文为准", confidence=0.8,
        provenance={"artifact_namespace": "research_reference", "source_record_id": source,
                    "read_id": original_read.read_id, "read_char_start": 0, "read_char_end": len(quote)})
    card = EvidenceCard(card_id="C-" + name, novelty_point_id=point.point_id, task_id=task.task_id,
        document_title="Synthetic Lumen method", main_contribution="提出图异常检测；具体聚合与阈值机制需对照原文。",
        sources=[EvidenceSource(title="Synthetic Lumen method", quote=quote,
                                location=f"artifact {texts[0][0]} chars:0-{len(quote)}")],
        evidence_ids=[evidence.evidence_id], relevance=0.8, confidence=0.8)
    return NoveltyPointReviewRequest(subject_paper_id=paper, novelty_point=point, tasks=[task],
                                     cards=[card], evidence=[evidence])


class LoggedLocalClient:
    def __init__(self, output):
        # Fixed loopback endpoint; dummy key for the unauthenticated local server.
        # No registry, environment profile, external credentials or fallback chain.
        self.profile = ModelProfile(alias="local-qwen2.5-7b", model="qwen2.5-7b-instruct",
                                    base_url="http://127.0.0.1:8000/v1", api_key="local", context_window=32768)
        self.inner = OpenAICompatibleChatClient(self.profile, fallback_profiles=())
        self.output, self.calls = output, []
        self.fail_next = False

    async def acomplete(self, messages, *, options=None):
        if self.fail_next:
            self.fail_next = False
            raise TimeoutError("injected summary transport timeout before dispatch")
        if len(self.calls) >= 64:
            raise ModelCallBudgetExceeded("local acceptance global 64-call limit")
        index = len(self.calls) + 1
        entry = {"index": index, "case": CASE.get(), "started_at": datetime.now(timezone.utc).isoformat()}
        self.calls.append(entry)  # Reserve before await: concurrent cases share the global cap.
        started = time.monotonic()
        payload = {"messages": [asdict(m) for m in messages], "options": asdict(options)}
        save(self.output / "calls" / f"{index:03d}-input.json", payload)
        try:
            response = await self.inner.acomplete(messages, options=options)
            entry["usage"] = dict(response.usage)
            entry["content"] = response.content
            entry["tool_calls"] = [asdict(call) for call in response.tool_calls]
            return response
        except Exception as exc:
            entry["error"] = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            entry["seconds"] = round(time.monotonic() - started, 3)
            save(self.output / "calls" / f"{index:03d}-result.json", entry)


async def run(output, *, checkpoint_only=False):
    started_at = time.monotonic()
    output.mkdir(parents=True, exist_ok=False)
    store = ReferenceStore(output / "workspace")
    client = LoggedLocalClient(output)
    prompts = PromptLibrary(ROOT / "backend/src/novelty_agent_framework/prompts")
    registry = ResearcherToolRegistry([ReviewerReaderTool(ReferenceArtifactReaderTool(store))])

    def service(*, reading=True, steps=10):
        return NoveltyEvidenceReviewer(client, prompts=prompts,
            config=EvidenceReviewerConfig(max_steps=steps, max_tool_calls=2, max_total_read_chars=8000,
                                          card_timeout_seconds=180),
            model_options=ModelCallOptions(temperature=0, max_tokens=2048, timeout_seconds=60),
            tool_registry=registry if reading else ResearcherToolRegistry())

    rows = []

    async def case(name, *, reading=True, complete=False, missing=False, steps=10, shared=None):
        request = fixture(store, name, complete=complete, missing=missing)
        save(output / name / "request.json", request.model_dump(mode="json"))
        token = CASE.set(name)
        started = time.monotonic()
        try:
            review = await (shared or service(reading=reading, steps=steps)).review_card(request)
        finally:
            CASE.reset(token)
        checks = {"no_point_verdict": not {"verdict", "confidence"}.intersection(review.model_dump()),
            "model_budget": review.harness_state["model_calls_used"] <= steps,
            "card_ownership": all(e.origin_card_id == request.cards[0].card_id for e in review.review_evidence),
            "runtime_outcome": (review.incomplete_reason == "budget_exhausted" if steps == 2
                                else review.status.value == "reviewed")}
        try:
            _validate_partial_review_reads(review, request.cards[0], request.evidence,
                                           paper_id=request.subject_paper_id, reference_store=store)
            checks["persisted_read_integrity"] = True
        except ValueError:
            checks["persisted_read_integrity"] = False
        if not reading:
            checks["no_unexecuted_reads"] = not review.review_evidence and not review.reader_observations
        if name == "fulltext":
            checks["actual_body_read"] = any(e.artifact_id == "body-fulltext" for e in review.review_evidence)
        if missing:
            checks["missing_body_not_registered"] = all(e.artifact_id != "body-" + name for e in review.review_evidence)
        relations = {x.feature_id: x.relation for x in review.feature_comparisons}
        expected = {"F1": "supported", "F2": "contradicted", "F3": "unknown"} if complete or name == "fulltext" else None
        if name == "unknown":
            expected = dict.fromkeys(("F1", "F2", "F3"), "unknown")
        row = {"case": name, "seconds": round(time.monotonic() - started, 3), "checks": checks,
            "semantic_expected": expected, "semantic_actual": relations,
            "semantic_match": relations == expected if expected else None,
            "review": review.model_dump(mode="json")}
        rows.append(row)
        save(output / name / "result.json", row)
        print(json.dumps({key: row[key] for key in ("case", "seconds", "checks", "semantic_match")}), flush=True)
        return row

    recovery = None
    if checkpoint_only:
        card_row = await case("checkpoint", reading=False, complete=True)
        request = NoveltyPointReviewRequest.model_validate_json((output / "checkpoint/request.json").read_text())
        indexed = [{"index": 1, "card_id": request.cards[0].card_id, "novelty_point_id": "NP-1",
                    "status": "completed", "review": card_row["review"]}]
        summary_service = service(reading=False)
        client.fail_next = True
        first = await summary_service.summarize_with_checkpoint(request, indexed,
            output_root=store.output_root, run_id="local-summary-recovery")
        save(output / "initial-summary-attempt.json", first.model_dump(mode="json"))
        if not first.durable:
            raise RuntimeError("synthetic checkpoint setup failed; see initial-summary-attempt.json")
        before = len(client.calls)
        saved = next(store.output_root.rglob("checkpoint.json"))
        checkpoint_bytes = saved.read_bytes()
        token = CASE.set("checkpoint-summary-recovery")
        try:
            recovered = await summary_service.recover_summary(request, output_root=store.output_root,
                run_id="local-summary-recovery", checkpoint_id=first.checkpoint_id)
        finally:
            CASE.reset(token)
        checks = {"initial_failure_durable": first.status == "failed" and first.durable,
            "recovery_completed": recovered.status == "completed",
            "frozen_facts_unchanged": recovered.partial_card_results == indexed and saved.read_bytes() == checkpoint_bytes,
            "recovery_no_tools": all(not json.loads((output / "calls" / f"{c['index']:03d}-input.json").read_text())
                                     ["options"]["tools"] for c in client.calls[before:])}
        card_row["checks"].update(checks)
        recovery = {"checks": checks, "initial": first.model_dump(mode="json"), "recovered": recovered.model_dump(mode="json")}
        save(output / "recovery.json", recovery)
    else:
        await case("available_input", reading=False, complete=True)
        await case("fulltext")
        await case("unknown", reading=False)
        await case("missing", missing=True)
        await case("budget", steps=2)
        shared = service()
        await asyncio.gather(*(case(f"concurrent-{n}", shared=shared) for n in range(4)))
    summary = {"endpoint": client.profile.base_url, "model": client.profile.model, "paid_model_calls": 0,
        "fixture_kind": "synthetic", "human_gold": False, "wall_seconds": round(time.monotonic() - started_at, 3),
        "call_count": len(client.calls), "prompt_tokens": sum(c.get("usage", {}).get("prompt_tokens", 0) for c in client.calls),
        "completion_tokens": sum(c.get("usage", {}).get("completion_tokens", 0) for c in client.calls),
        "all_runtime_checks_passed": all(all(row["checks"].values()) for row in rows),
        "semantic_cases_matched": sum(row["semantic_match"] is True for row in rows),
        "semantic_cases_checked": sum(row["semantic_match"] is not None for row in rows),
        "recovery": recovery,
        "cases": rows, "calls": [{k: v for k, v in c.items() if k not in {"content", "tool_calls"}} for c in client.calls]}
    save(output / "summary.json", summary)
    print(json.dumps({k: v for k, v in summary.items() if k not in {"cases", "calls", "recovery"}}), flush=True)
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="Explicitly call only the fixed local endpoint")
    parser.add_argument("--output", type=Path, required=True, help="New output directory; existing data is never overwritten")
    parser.add_argument("--checkpoint-only", action="store_true", help="One real card and summary recovery after an injected failure")
    args = parser.parse_args()
    if not args.live:
        parser.error("--live is required; no model is called without it")
    result = asyncio.run(run(args.output.resolve(), checkpoint_only=args.checkpoint_only))
    raise SystemExit(0 if result["all_runtime_checks_passed"] else 1)
