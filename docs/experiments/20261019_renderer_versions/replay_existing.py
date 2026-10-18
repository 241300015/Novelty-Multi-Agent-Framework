"""Import audited historical snapshots for offline Renderer acceptance only.

This is not a production migration or proof of historical write-time hashes.
Business files stay byte-identical; only test copies receive a version index.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
import subprocess
import sys

import validate_materials as matrix

from novelty_agent_framework.core.integrity_gates import validate_report_integrity
from novelty_agent_framework.persistence import ReferenceStore
from novelty_agent_framework.report_inputs import (
    ARTIFACT_FILES, INDEX_NAME, begin_report_inputs, record_report_input_files,
    finalize_report_inputs, load_verified_report_inputs,
)
from novelty_agent_framework.schemas import (
    PaperInput, NoveltyPoint, NoveltyPointReview, NoveltyReport, EvidenceCard,
    ResearchTask, SearchPlan, ReferenceManifest, Evidence,
)


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def audit_source(run):
    manifest = matrix.read(run / "run.json")
    assert manifest["status"] == "SUCCESS"
    workspace = run / manifest["paper_id"]
    runtime = workspace / "runtime" / manifest["runtime_run_id"]
    summary = matrix.read(runtime / "summary.json")
    assert summary["run"]["status"] == "SUCCESS"
    assert summary["run"]["run_id"] == manifest["runtime_run_id"]
    assert summary["run"]["input_identity"]["paper_sha256"] == manifest["paper_sha256"]
    stage = sorted((runtime / "stages").glob("*_persist_report"))[-1]
    assert matrix.read(stage / "meta.json")["status"] == "SUCCESS"
    state = matrix.read(stage / "input.json")
    assert state["run_id"] == manifest["runtime_run_id"]
    files = {name: matrix.read(workspace / name) for _, name, _, _ in ARTIFACT_FILES}
    assert files["paper-input/others/paper.json"] == state["paper"]
    assert files["novelty-points.json"]["novelty_points"] == state["novelty_points"]
    assert files["novelty-reviews.json"]["reviews"] == state["novelty_reviews"]
    assert files["report.json"] == state["report"]
    for name, key in (("raw_evidence_cards", "raw_evidence_cards"),
                      ("accepted_evidence_cards", "evidence_cards"),
                      ("validator_accepted_cards", "validator_accepted_cards"),
                      ("rejected_evidence", "rejected_evidence")):
        assert files["evidence-cards.json"][name] == state[key]
    plans = files["retrieval-plans.json"]
    tasks = [task for row in plans["novelty_point_plans"] for task in row["research_tasks"]]
    search_plans = [plan for row in plans["novelty_point_plans"] for plan in row["search_plans"]]
    canonical = lambda values: sorted(json.dumps(v, sort_keys=True) for v in values)
    assert canonical(tasks) == canonical(state["all_research_tasks"])
    assert canonical(search_plans) == canonical(state["search_plans"])
    assert plans["rounds"] == state["rounds"]
    for task in tasks:
        ResearchTask.model_validate(task)
    for plan in search_plans:
        SearchPlan.model_validate(plan)
    for name in ("references/list.json", "subject_references/list.json"):
        ReferenceManifest.model_validate(files[name])
    paper = PaperInput.model_validate(state["paper"])
    points = [NoveltyPoint.model_validate(v) for v in state["novelty_points"]]
    reviews = [NoveltyPointReview.model_validate(v) for v in state["novelty_reviews"]]
    cards = [EvidenceCard.model_validate(v) for v in state["evidence_cards"]]
    report = NoveltyReport.model_validate(state["report"])
    raw_evidence = [Evidence.model_validate(v) for v in state["raw_evidence"]]
    integrity = validate_report_integrity(
        report, novelty_points=points, evidence_cards=cards, novelty_reviews=reviews,
        evidence=raw_evidence, reference_store=ReferenceStore(output_root=run),
    ).audit()
    assert integrity["validation_passed"], integrity
    assert paper.paper_id == manifest["paper_id"]
    for stage_name in ("synthesize_report", "validate_report_integrity", "persist_report", "render_report"):
        assert any(row["stage_name"] == stage_name and row["status"] == "SUCCESS"
                   for row in summary["stages"]), stage_name
    names = [*(name for _, name, _, _ in ARTIFACT_FILES), "paper-input/full.md"]
    hashes = {name: matrix.digest(workspace / name) for name in names}
    return manifest, workspace, runtime, {
        "original_run": matrix.relative(run), "original_commit": manifest["git_commit"],
        "original_run_id": manifest["runtime_run_id"],
        "current_schema_validation": "PASS", "current_report_integrity": integrity,
        "business_files_match_final_runtime_state": True,
        "original_file_sha256": hashes,
        "historical_models": summary["llm_usage"]["by_model"],
        "accepted_card_count": len(cards),
        "original_evidence_count": len(raw_evidence),
        "partial_card_review_count": len(report.partial_card_reviews),
        "limitations": "No historical write-time version index exists. Original review statuses and limitations are preserved; successful workflow execution does not imply completed novelty evaluation.",
    }


def import_snapshot(audited, destination):
    manifest, source, runtime, audit = audited
    workspace = destination / manifest["paper_id"]
    workspace.mkdir(parents=True)
    for name, expected_hash in audit["original_file_sha256"].items():
        target = workspace / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source / name, target)
        assert matrix.digest(target) == expected_hash
    # Runtime metadata and the final state establish provenance independently
    # of the version index that is newly created for this acceptance replay.
    for path in [runtime / "summary.json", *sorted((runtime / "stages").glob("*_persist_report/*.json"))]:
        target = workspace / path.relative_to(source)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
    shutil.copy2(source.parent / "run.json", destination / "run.json")
    begin_report_inputs(workspace, manifest["paper_id"], manifest["runtime_run_id"])
    names = tuple(audit["original_file_sha256"])
    record_report_input_files(workspace, names, run_id=manifest["runtime_run_id"])
    finalize_report_inputs(workspace, run_id=manifest["runtime_run_id"])
    index = matrix.read(workspace / INDEX_NAME)
    # Material age belongs to the historic run, not today's test import.
    imported_at = datetime.now(timezone.utc).isoformat()
    index["started_at"] = manifest["started_at"]
    index["materials_as_of"] = manifest["finished_at"]
    index["test_snapshot_import"] = {"imported_at": imported_at,
                                     "source_run": audit["original_run"],
                                     "source_commit": manifest["git_commit"],
                                     "historical_write_time_index": False}
    write(workspace / INDEX_NAME, index)
    load_verified_report_inputs(workspace)
    for name, expected_hash in audit["original_file_sha256"].items():
        assert matrix.digest(workspace / name) == expected_hash
    write(destination / "import-audit.json", audit)
    return destination


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-a", required=True, type=Path)
    parser.add_argument("--run-b", required=True, type=Path)
    parser.add_argument("--work-root", required=True, type=Path)
    parser.add_argument("--result", required=True, type=Path)
    args = parser.parse_args()
    assert not args.work_root.exists(), "Use a new work root to preserve previous evidence."
    audited = [audit_source(run.resolve()) for run in (args.run_a, args.run_b)]
    assert audited[0][0]["git_commit"] == audited[1][0]["git_commit"], "Source architecture commits differ"
    assert audited[0][0]["paper_sha256"] == audited[1][0]["paper_sha256"]
    imported = [import_snapshot(row, args.work_root.resolve() / "imported" / label)
                for label, row in zip(("A", "B"), audited)]
    matrix.validate(argparse.Namespace(run_a=imported[0], run_b=imported[1],
                                      cases_root=args.work_root / "cases", result=args.result))
    result = matrix.read(args.result)
    for label, row in zip(("A", "B"), audited):
        audit = row[3]
        for name, expected_hash in audit["original_file_sha256"].items():
            assert matrix.digest(row[1] / name) == expected_hash, "Source mutated"
        result["sources"][label]["historical_import"] = audit
    result["test_commit"] = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    result["scope"] = "Offline audit, snapshot import, gate, default Renderer, repeated rendering and mixed-version rejection. No new agent execution; historical full workflows had SUCCESS status."
    result["network_calls"] = 0
    result["model_calls"] = 0
    result["source_business_files_unchanged"] = True
    write(args.result, result)


if __name__ == "__main__":
    def deny_network(event, args):
        if event in {"socket.connect", "socket.getaddrinfo", "socket.sendto"}:
            raise RuntimeError("Network is forbidden during offline acceptance")
    sys.addaudithook(deny_network)
    main()
