"""Verify frozen report lineage and create a controlled local publication bundle."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from pathlib import Path

from novelty_agent_framework.services.report_resources import verify_bundle


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-manifest", type=Path, required=True)
    parser.add_argument("--live-index", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--label", required=True)
    args = parser.parse_args()
    sources = {row["label"]: row for row in json.loads(args.source_manifest.read_text())["sources"]}
    results = {row["label"]: row for row in json.loads(args.live_index.read_text())}
    source, result = sources[args.label], results[args.label]
    if source["source_run_id"] != result["source_run_id"] or source["source_run_status"] != "FAILED":
        raise ValueError("source identity or original failure status differs")
    input_path = Path(source["synthesize_stage_input"])
    response_path = Path(result["live_response_file_local"])
    report_path = Path(result["final_report_json"])
    markdown_path = Path(result["final_report_markdown"])
    checks = ((input_path, source["synthesize_stage_input_sha256"]),
              (response_path, result["live_response_sha256"]),
              (report_path, result["final_report_json_sha256"]),
              (markdown_path, result["final_report_markdown_sha256"]))
    for path, expected in checks:
        if sha(path) != expected:
            raise ValueError(f"frozen file hash mismatch: {path}")
    raw = json.loads(input_path.read_text())
    source_run_manifest = json.loads((input_path.parents[2] / "manifest.json").read_text())
    if (source_run_manifest.get("status") != "FAILED"
            or source_run_manifest.get("run_id") != source["runtime_run_id"]
            or source_run_manifest.get("paper_id") != source["paper_id"]):
        raise ValueError("original source run manifest does not match")
    report = json.loads(report_path.read_text())
    if raw["paper"]["paper_id"] != report["paper_id"] or report["paper_id"] != source["paper_id"]:
        raise ValueError("paper identity differs")
    output = args.output_root / args.label
    if output.exists():
        raise FileExistsError(output)
    output.mkdir(parents=True)
    shutil.copyfile(report_path, output / "report.json")
    shutil.copyfile(markdown_path, output / "report.md")
    shutil.copyfile(response_path, output / "live-response.json")
    shutil.copyfile(input_path, output / "synthesis-input.json")
    audit = {
        "paper_id": report["paper_id"],
        "source_input_sha256": source["synthesize_stage_input_sha256"],
        "source_run_manifest": source_run_manifest,
        "novelty_points": raw["novelty_points"],
        "novelty_reviews": raw["novelty_reviews"],
        "evidence_cards": raw["evidence_cards"],
        "rejected_evidence": raw.get("rejected_evidence", []),
    }
    (output / "audit.json").write_text(json.dumps(audit, ensure_ascii=False, indent=2) + "\n")
    issues = [
        {"point_id": review["novelty_point_id"], "review_status": review["status"],
         "incomplete_reason": review.get("incomplete_reason")}
        for review in raw["novelty_reviews"]
    ]
    provenance = {
        "source_run_id": source["source_run_id"],
        "source_runtime_id": source["runtime_run_id"],
        "source_run_status": "FAILED",
        "live_recovery_id": result["live_recovery_id"],
        "assembly_id": json.loads((report_path.parents[1] / "recovery-result.json").read_text())["recovery_id"],
        "report_kind": "captured_response_reassembly",
        "paper_id": report["paper_id"],
        "upstream_recomputed": False,
        "semantic_status": "inherited_not_reassessed",
        "source_issues": issues,
    }
    (output / "provenance.json").write_text(json.dumps(provenance, ensure_ascii=False, indent=2) + "\n")
    files = {}
    for name, mime in (("report.md", "text/markdown"), ("report.json", "application/json"),
                       ("provenance.json", "application/json")):
        path = output / name
        files[name] = {"sha256": sha(path), "bytes": path.stat().st_size, "mime": mime}
    manifest = {
        **provenance,
        "source_input_sha256": source["synthesize_stage_input_sha256"],
        "live_response_sha256": result["live_response_sha256"],
        "files": files,
    }
    (output / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    verify_bundle(output)
    print(json.dumps({"label": args.label, "bundle": str(output),
                      "source_run_id": source["source_run_id"],
                      "live_response_sha256": result["live_response_sha256"],
                      "files": files}, ensure_ascii=False))


if __name__ == "__main__":
    main()
