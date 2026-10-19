"""Recheck both frozen source chains and emit redacted transfer indexes."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import tempfile
from pathlib import Path

from novelty_agent_framework.services.report_resources import verify_bundle


def _record(path: Path) -> dict:
    data = path.read_bytes()
    return {"local_path": str(path), "bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest()}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-manifest", type=Path, required=True)
    parser.add_argument("--live-index", type=Path, required=True)
    parser.add_argument("--bundle-root", type=Path, required=True)
    parser.add_argument("--registry", type=Path, required=True)
    parser.add_argument("--archive", type=Path, required=True)
    args = parser.parse_args()
    args.archive.mkdir(parents=True, exist_ok=True)
    source = {r["label"]: r for r in json.loads(args.source_manifest.read_text())["sources"]}
    live = json.loads(args.live_index.read_text())
    indexes = []
    registry = {json.loads(p.read_text())["source_run_id"]: p.parent.name
                for p in args.registry.glob("rr-*/manifest.json")}
    for item in live:
        label = item["label"]
        row = source[label]
        bundle = args.bundle_root / label
        with tempfile.TemporaryDirectory() as temp:
            relocated = Path(temp) / "bundle"
            shutil.copytree(bundle, relocated)
            verify_bundle(relocated)
        checks = (
            ("source_input", row["synthesize_stage_input"], row["synthesize_stage_input_sha256"]),
            ("live_response", item["live_response_file_local"], item["live_response_sha256"]),
            ("final_json", item["final_report_json"], item["final_report_json_sha256"]),
            ("final_markdown", item["final_report_markdown"], item["final_report_markdown_sha256"]),
        )
        files = []
        for role, path, expected in checks:
            record = _record(Path(path))
            record.update(role=role, expected_sha256=expected,
                          match=record["sha256"] == expected)
            files.append(record)
        if not all(record["match"] for record in files):
            raise ValueError(f"frozen source hash mismatch: {label}")
        indexes.append({
            "label": label, "source_run_id": row["source_run_id"],
            "source_runtime_id": row["runtime_run_id"], "source_run_status": "FAILED",
            "live_recovery_id": item["live_recovery_id"],
            "assembly_id": json.loads((bundle / "provenance.json").read_text())["assembly_id"],
            "report_resource_id": registry[row["source_run_id"]],
            "all_expected_hashes_match": True,
            "portable_bundle_verification": "passed",
            "files": files,
            "bundle_files": [{"role": p.name, **_record(p)}
                             for p in sorted(bundle.iterdir()) if p.is_file()],
            "point_ids": item["point_ids"],
        })
    (args.archive / "real-source-index.json").write_text(
        json.dumps(indexes, ensure_ascii=False, indent=2) + "\n")
    (args.archive / "verification-results.json").write_text(json.dumps({
        "sources_verified": len(indexes), "all_expected_hashes_match": True,
        "portable_bundle_verification": "passed_for_both", "new_business_calls": 0,
    }, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"sources_verified": len(indexes), "all_expected_hashes_match": True}))


if __name__ == "__main__":
    main()
