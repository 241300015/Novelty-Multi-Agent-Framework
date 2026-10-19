"""真实 Coordinator 综述重放；固定历史上游材料，不重新检索或评审。"""
from __future__ import annotations

import argparse
from dataclasses import asdict, replace
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import time
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT / "backend/src"), str(ROOT)]

from novelty_agent_framework.agents.coordinator import NoveltyCoordinatorAgent
from backend.env import ModelProfile, OpenAICompatibleChatClient
from novelty_agent_framework.config.factory import build_prompt_library, _typed_model_options
from novelty_agent_framework.config.loader import load_application_config
from novelty_agent_framework.persistence import persist_report
from novelty_agent_framework.report_inputs import (
    ARTIFACT_FILES, INDEX_NAME, begin_report_inputs, load_verified_report_inputs,
    record_report_input_files,
)
from novelty_agent_framework.schemas import EvidenceCard, NoveltyPoint, NoveltyPointReview, NoveltyReport, PaperInput
from novelty_agent_framework.tools.renderer import render_report


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


class CaptureClient:
    def __init__(self, client, directory):
        self.client, self.directory, self.calls = client, directory, 0

    def complete(self, messages, *, options=None):
        if self.calls >= 2:
            raise RuntimeError("Experiment per-run call budget exhausted")
        self.calls += 1
        prefix = self.directory / f"call-{self.calls}"
        save(prefix.with_suffix(".request.json"), {
            "messages": [asdict(message) for message in messages], "options": asdict(options),
        })
        started = time.monotonic()
        response = self.client.complete(messages, options=options)
        save(prefix.with_suffix(".response.json"), {
            **asdict(response), "elapsed_seconds": round(time.monotonic() - started, 3),
        })
        return response


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--destination", required=True, type=Path)
    parser.add_argument("--live", action="store_true", help="实际发送请求；默认仅验证输入")
    args = parser.parse_args()
    config = load_application_config(overrides={"coordinator": {"model": {"alias": "local-qwen2.5-7b"}}})
    alias = config.coordinator.model.alias
    model = config.models[alias]
    if urlparse(model.base_url).hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("This experiment only sends requests to a local model")
    options = replace(_typed_model_options(config.coordinator.model), timeout_seconds=120, max_tokens=4096, extra_body={})
    fixtures = Path(__file__).parent / "results"
    prepared = []
    for case in [4, 4, 3, 2]:
        source = fixtures / f"case-{case}" / "MF2033k6lC"
        index, verified = load_verified_report_inputs(source)
        read = lambda name: json.loads(verified[name])
        paper = PaperInput.model_validate(read("paper-input/others/paper.json"))
        points = [NoveltyPoint.model_validate(p) for p in read("novelty-points.json")["novelty_points"]]
        cards = [EvidenceCard.model_validate(c) for c in read("evidence-cards.json")["accepted_evidence_cards"]]
        reviews = [NoveltyPointReview.model_validate(r) for r in read("novelty-reviews.json")["reviews"]]
        old = NoveltyReport.model_validate(read("report.json"))
        prepared.append((case, source, index, verified, paper, points, cards, reviews, old))
    if not args.live:
        print(json.dumps({"preflight": "passed", "runs": len(prepared), "model": model.model,
                          "points_cards": [[len(p[5]), len(p[6])] for p in prepared]}))
        return
    args.destination.mkdir(parents=True, exist_ok=False)
    source_files = [Path(__file__), ROOT / "backend/src/novelty_agent_framework/prompts/coordinator/synthesize.md",
                    ROOT / "backend/src/novelty_agent_framework/response_language.py",
                    ROOT / "backend/src/novelty_agent_framework/agents/coordinator.py",
                    ROOT / "backend/src/novelty_agent_framework/core/report_binding.py",
                    ROOT / "backend/src/novelty_agent_framework/tools/renderer.py",
                    ROOT / "backend/src/novelty_agent_framework/templates/markdown/concise.md"]
    manifest = {"scope": "live coordinator synthesis only; frozen historical upstream evidence/reviews",
                "status_scope": "automated mechanical checks only; semantic quality requires separate review",
                "started_at": datetime.now(timezone.utc).isoformat(), "model_alias": alias,
                "model": model.model, "base_url": model.base_url, "options": asdict(options),
                "max_model_calls": 8, "source_sha256": {}, "runs": []}
    for path in source_files:
        relative = path.resolve().relative_to(ROOT)
        target = args.destination / "source" / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(path.read_bytes())
        manifest["source_sha256"][str(relative)] = sha(path.read_bytes())
    save(args.destination / "results.json", manifest)
    local_client = OpenAICompatibleChatClient(ModelProfile(
        alias=alias, model=model.model, base_url=model.base_url,
        api_key=os.getenv("LOCAL_VLLM_API_KEY") or "local-probe",
        context_window=model.context_window,
    ))
    for number, (case, source, index, verified, paper, points, cards, reviews, old) in enumerate(prepared, 1):
        directory = args.destination / f"run-{number}-case-{case}"
        directory.mkdir()
        run_id = f"p0-07-live-synthesis-{args.destination.name}-{number}"
        client = CaptureClient(local_client, directory)
        agent = NoveltyCoordinatorAgent(model_client=client, prompts=build_prompt_library(),
            model_options=options, research_languages=("en",),
            synthesis_prompt_name=config.coordinator.prompt_names.synthesize)
        brief = agent.plan(paper, points=points, attempt=1)
        row = {"run": number, "case": case, "run_id": run_id,
               "source": str(source.relative_to(ROOT)), "source_run_id": index["run_id"],
               "input_sha256": {name: sha(raw) for name, raw in verified.items()},
               "points": len(points), "cards": len(cards)}
        save(directory / "input-manifest.json", {**row,
             "brief_origin": "deterministically rebuilt by Coordinator.plan; English search tasks",
             "insufficient_final_evidence_points": [],
             "gap_note": "Historical snapshot has no aggregate count-gap object; none invented. Reviewer gaps remain intact."})
        print(f"START run={number} case={case} points={len(points)} cards={len(cards)}", flush=True)
        try:
            report = agent.synthesize(paper, brief=brief, evidence=cards, novelty_reviews=reviews,
                rejected_evidence=json.loads(verified["evidence-cards.json"])["rejected_evidence"],
                insufficient_final_evidence_points=[])
            # Carry forward recorded upstream state; only synthesis is newly executed.
            values = old.model_dump(mode="json")
            values["conclusions"] = [c.model_dump(mode="json") for c in report.conclusions]
            values["limitations"] = list(dict.fromkeys([*old.limitations, *report.limitations]))
            report = NoveltyReport.model_validate(values)
            workspace = directory / source.name
            for relative, raw in verified.items():
                if relative == "report.json":
                    continue
                target = workspace / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(raw)
            begin_report_inputs(workspace, paper.paper_id, run_id)
            ordered = [relative for _, relative, _, _ in ARTIFACT_FILES if relative in verified and relative != "report.json"]
            ordered += [relative for relative in verified if relative not in ordered and relative != "report.json"]
            record_report_input_files(workspace, tuple(ordered), run_id=run_id)
            persist_report(paper, report, output_root=directory, run_id=run_id)
            for template in ("default", "concise"):
                render_report(paper_name=source.name, output_root=directory, template_name=template,
                    save_path=directory / f"{template}.md", expected_run_id=run_id)
            text = (directory / "concise.md").read_text()
            summaries = [c.summary for c in report.conclusions]
            fields = {"review_status": "status", **{key: key for key in (
                "verdict", "verdict_reason", "confidence", "highly_relevant_works", "review_evidence",
                "feature_comparisons", "incomplete_reason")}}
            checks = {
                "review_authority_unchanged": all(getattr(c, target) == getattr(r, origin)
                    for c, r in zip(report.conclusions, reviews) for target, origin in fields.items()),
                "summary_present_in_display": all(s in text for s in summaries),
                "all_summaries_have_chinese": all(re.search(r"[\u4e00-\u9fff]", s) for s in summaries),
                "no_technical_prose": not any(term in "\n".join(summaries) for term in (
                    "Reviewer", "Gate", "NP-", "technical_error", "预算", "错误码", "工具调用", "补检次数")),
                "no_display_debug": not any(term in text for term in (
                    "本次补充检索上限", "执行问题", "完整性检查", "[原文]", "{{")),
                "original_inputs_unchanged": load_verified_report_inputs(source)[1] == verified,
            }
            row.update(status="passed" if all(checks.values()) else "check_failed", checks=checks)
            save(directory / "summaries.json", [c.model_dump(mode="json", include={"novelty_point_id", "summary"}) for c in report.conclusions])
        except Exception as exc:
            row.update(status="error", error_type=type(exc).__name__, error=str(exc))
        row["model_calls"] = client.calls
        manifest["runs"].append(row)
        save(args.destination / "results.json", manifest)
        print(json.dumps({k: row[k] for k in ("run", "case", "status", "model_calls")}), flush=True)
    manifest["finished_at"] = datetime.now(timezone.utc).isoformat()
    save(args.destination / "results.json", manifest)
    if any(row["status"] != "passed" for row in manifest["runs"]):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
