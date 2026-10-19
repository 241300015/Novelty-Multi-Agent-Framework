"""Fixed real-source Reviewer evaluation; no retrieval or schema changes.

Run with PYTHONPATH=backend/src:. .venv/bin/python -m
novelty_agent_framework.experiments.reviewer_benchmark --output PATH [--live].
Default mode only validates fixtures; it never fabricates model verdicts.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import time
from datetime import datetime, timezone
from pathlib import Path

from backend.env import PromptLibrary
from novelty_agent_framework.agents import DefaultEvidenceValidator
from novelty_agent_framework.config import build_workflow, load_application_config
from novelty_agent_framework.schemas import EvidenceCard, EvidenceSource, NoveltyPoint, ResearchTask

ROOT = Path(__file__).resolve().parents[4]
FIXTURE = ROOT / "tests/fixtures/reviewer/benchmark.json"
VERDICTS = ("accept", "reject", "needs_more_evidence")


def load_cases(path=FIXTURE):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    seen = set()
    cases = []
    for item in data["cases"]:
        if item["id"] in seen:
            raise ValueError("duplicate case id")
        seen.add(item["id"])
        source = data["sources"][item["source"]]
        text = (ROOT / source["path"]).read_text(encoding="utf-8")
        quote = source["quote"]
        start = text.index(quote)  # fail if archived grounding has changed
        point = NoveltyPoint(point_id=item["id"], claim=item["claim"])
        task = ResearchTask(task_id=item["id"], novelty_point_id=item["id"],
                            task_type="literature_search", language="en", description=item["claim"])
        card = EvidenceCard(
            card_id=item["id"], task_id=task.task_id, novelty_point_id=point.point_id,
            document_title=source["title"], main_contribution=item["contribution"],
            overlaps=item.get("overlaps", []), differences=item.get("differences", []),
            sources=[EvidenceSource(title=source["title"], quote=quote,
                url=None if item.get("omit_url") else source["url"],
                location=None if item.get("omit_location") else f"archived text chars:{start}-{start+len(quote)}")],
            confidence=0.9, relevance=0.9,
        )
        cases.append((item, card, point, task, hashlib.sha256(text.encode()).hexdigest()))
    return data, cases


def metrics(rows):
    eligible = [r for r in rows if r["expected_verdict"] in VERDICTS]
    valid = [r for r in eligible if not r.get("runtime_error") and r.get("actual_verdict") in VERDICTS]
    matrix = {a: {b: 0 for b in VERDICTS} for a in VERDICTS}
    for row in valid:
        matrix[row["expected_verdict"]][row["actual_verdict"]] += 1
    positives = [r for r in valid if r["expected_verdict"] == "accept"]
    negatives = [r for r in valid if r["expected_verdict"] == "reject"]
    def rate(count, denominator):
        return count / denominator if denominator else None
    false_rejects = sum(r["actual_verdict"] == "reject" for r in positives)
    missed_rejects = sum(r["actual_verdict"] != "reject" for r in negatives)
    return {
        "eligible": len(eligible), "valid_decisions": len(valid),
        "runtime_errors": sum(bool(r.get("runtime_error")) for r in eligible),
        "confusion_matrix": matrix,
        "agreement": rate(sum(r["actual_verdict"] == r["expected_verdict"] for r in valid), len(valid)),
        "false_reject_count": false_rejects, "false_reject_denominator": len(positives),
        "false_reject_rate": rate(false_rejects, len(positives)),
        "missed_reject_count": missed_rejects, "missed_reject_denominator": len(negatives),
        "missed_reject_rate": rate(missed_rejects, len(negatives)),
        "unsafe_accept_count": sum(r["actual_verdict"] == "accept" for r in negatives),
        "confidence_out_of_range": sum(not r["confidence_in_range"] for r in valid),
    }


def run(output, *, live=False, repeats=1, prompt_root=None, prompt_name=None):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    data, cases = load_cases()
    config = load_application_config(environ={})
    config.reviewer.enabled = True
    if prompt_name:
        config.reviewer.prompt = prompt_name
    reviewer = build_workflow(config).services.reviewer
    if prompt_root:
        reviewer._prompts = PromptLibrary(Path(prompt_root))
    calls = []
    if live:
        client = reviewer._client()
        if not client.profile.api_key:
            (output / "blocked.json").write_text(json.dumps({
                "status": "blocked", "reason": "missing_model_credential",
                "model_alias": config.reviewer.model.alias,
                "required_env": config.models[config.reviewer.model.alias].api_key_env,
                "model_calls": 0,
            }, indent=2))
            raise RuntimeError("Configured Reviewer model credential is unavailable")

        class RecordingClient:
            def complete(self, messages, *, options=None):
                record = {"messages": [{"role": m.role, "content": m.content} for m in messages],
                          "temperature": options.temperature, "max_tokens": options.max_tokens,
                          "timeout_seconds": options.timeout_seconds}
                start = time.monotonic()
                try:
                    response = client.complete(messages, options=options)
                    record.update(response=response.content, usage=dict(response.usage))
                    return response
                except Exception as exc:
                    record["error_type"] = type(exc).__name__
                    raise
                finally:
                    record["elapsed_seconds"] = time.monotonic() - start
                    calls.append(record)
                    (output / "model_calls.json").write_text(json.dumps(calls, ensure_ascii=False, indent=2))
        reviewer.model_client = RecordingClient()

    rows = []
    for repeat in range(repeats):
        eligible = []
        batch_rows = []
        for item, card, point, task, source_hash in cases:
            validation = DefaultEvidenceValidator().validate_fields([card], tasks=[task])
            row = {**item, "repeat": repeat + 1, "source_sha256": source_hash,
                   "card": card.model_dump(mode="json"),
                   "validator_scope": "fields_only_synthetic_fixture",
                   "actual_validator": "accept" if validation.accepted else "reject",
                   "actual_verdict": None, "runtime_error": None}
            if row["actual_validator"] != item["expected_validator"]:
                raise AssertionError(f"Validator baseline changed: {item['id']}")
            if validation.accepted:
                eligible.append((card, point, task))
            batch_rows.append(row)
        if live:
            result = reviewer.review([x[0] for x in eligible], points=[x[1] for x in eligible], tasks=[x[2] for x in eligible])
            decisions = {d.card_id: d for d in result.decisions}
            for row in batch_rows:
                if row["actual_validator"] == "reject":
                    continue
                decision = decisions.get(row["id"])
                if decision is None:
                    row["runtime_error"] = "missing_or_invalid_decision"
                    continue
                if any("review_failed:" in i.message for i in decision.issues):
                    row["runtime_error"] = "model_call_or_parse_failure"
                    continue
                row["actual_verdict"] = decision.verdict.value
                row["decision"] = decision.model_dump(mode="json")
                low, high = row["confidence_range"]
                row["confidence_in_range"] = low <= decision.reviewed_confidence <= high
        rows.extend(batch_rows)
        (output / "cases.json").write_text(json.dumps(rows, ensure_ascii=False, indent=2))
        print(f"repeat {repeat + 1}/{repeats} finished", flush=True)
    summary = {
        "mode": "live" if live else "fixtures_only", "timestamp": datetime.now(timezone.utc).isoformat(),
        "annotation_status": data["annotation_status"],
        "fixture_sha256": hashlib.sha256(FIXTURE.read_bytes()).hexdigest(),
        "reviewer_config": config.reviewer.model_dump(mode="json"),
        "model": config.models[config.reviewer.model.alias].model,
        "prompt_sha256": hashlib.sha256(reviewer._prompts.render(
            config.reviewer.prompt,
            today="DATE",
            points_json="[]",
            novelty_point_json="{}",
            tasks_json="[]",
            cards_json="[]",
            evidence_json="[]",
            review_schema="{}",
        ).system.encode()).hexdigest(),
        "metrics": metrics(rows), "repeats": repeats,
        "validator_rejected": sum(r["actual_validator"] == "reject" for r in rows),
        "human_gold_metrics": False,
        "repeat_verdict_agreement": {
            item["id"]: len({r["actual_verdict"] for r in rows if r["id"] == item["id"]}) == 1
            for item, *_ in cases
            if live and repeats > 1 and all(r.get("actual_verdict") in VERDICTS for r in rows if r["id"] == item["id"])
        },
    }
    (output / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2))
    lines = ["# Reviewer benchmark", "", f"Mode: {summary['mode']}; labels: {data['annotation_status']}",
             "", "These are agreement measurements against proposed labels, not human-gold accuracy.",
             "", "| Case | Repeat | Expected | Actual | Validator | Error |", "|---|---:|---|---|---|---|"]
    for row in rows:
        lines.append(f"| {row['id']} | {row['repeat']} | {row['expected_verdict']} | {row['actual_verdict']} | {row['actual_validator']} | {row['runtime_error']} |")
    lines.extend(["", "```json", json.dumps(summary["metrics"], indent=2), "```", "", "## Proposed-label rationale"])
    for item, *_ in cases:
        lines.append(f"- {item['id']}: {item['rationale']}")
    (output / "report.md").write_text("\n".join(lines) + "\n")
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--prompt-root", type=Path)
    parser.add_argument("--prompt-name", help="Override typed Reviewer prompt for a controlled comparison")
    args = parser.parse_args()
    if args.repeats < 1:
        parser.error("--repeats must be positive")
    result = run(args.output, live=args.live, repeats=args.repeats,
                 prompt_root=args.prompt_root, prompt_name=args.prompt_name)
    print(json.dumps(result["metrics"], indent=2))
    if args.live and result["metrics"]["valid_decisions"] != result["metrics"]["eligible"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
