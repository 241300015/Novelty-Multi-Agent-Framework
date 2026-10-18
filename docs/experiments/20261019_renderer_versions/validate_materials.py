"""Replay two genuine completed runs and reject a mixture, without re-registering versions."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile
import time
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT / "backend/src"), str(ROOT)]

from novelty_agent_framework.report_inputs import INDEX_NAME, load_verified_report_inputs
from novelty_agent_framework.tools.renderer import ReportRenderError, render_report


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def relative(path):
    return os.path.relpath(Path(path).resolve(), ROOT)


def compile_one(workspace):
    destination = workspace / "report/acceptance.md"
    try:
        result = render_report(paper_name=workspace.name, output_root=workspace.parent,
                               save_path=destination)
    except ReportRenderError as exc:
        print(json.dumps({"status": "rejected", "error": str(exc),
                          "report_created": destination.exists()}, ensure_ascii=False))
        return 2
    text = result.read_text(encoding="utf-8")
    attachments = re.findall(r"\[查看文件\]\(<([^>]+)>\)", text)
    print(json.dumps({"status": "rendered", "report": relative(result),
                      "sha256": digest(result),
                      "attachment_count": len(attachments),
                      "missing_attachment_links": [target for target in attachments
                                                   if not (result.parent / unquote(target)).is_file()],
                      "unfilled_placeholders": re.findall(r"\{\{\s*[A-Za-z_]\w*\s*\}\}", text),
                      "times": [line for line in text.splitlines()
                                if line.startswith(("| 报告生成时间", "| 材料所属时间"))]}, ensure_ascii=False))
    return 0


def subprocess_compile(workspace):
    completed = subprocess.run([sys.executable, str(Path(__file__).resolve()),
                                "--compile", str(workspace)], cwd=ROOT,
                               capture_output=True, text=True, timeout=60)
    if completed.returncode not in (0, 2):
        raise RuntimeError(completed.stderr[-3000:])
    return json.loads(completed.stdout)


def source_details(run):
    manifest = read(run / "run.json")
    assert manifest["status"] == "SUCCESS", (run, manifest["status"])
    workspace = run / manifest["paper_id"]
    version, _ = load_verified_report_inputs(workspace)
    assert version["run_id"] == manifest["runtime_run_id"]
    for name in ("candidate-audit.json", "novelty-reviews.json", "evidence-cards.json"):
        assert version["artifacts"].get(name), f"incomplete stage: {name}"
    summary = read(next((workspace / "runtime").glob("*/summary.json")))
    evidence = read(workspace / "evidence-cards.json")
    reviews = {item["novelty_point_id"]: item for item in read(workspace / "novelty-reviews.json")["reviews"]}
    conclusions = read(workspace / "report.json")["conclusions"]
    for conclusion in conclusions:
        review = reviews[conclusion["novelty_point_id"]]
        assert conclusion["review_status"] == review["status"]
        for key in ("verdict", "verdict_reason", "confidence", "incomplete_reason"):
            assert conclusion.get(key) == review.get(key), (key, conclusion["novelty_point_id"])
    return workspace, {
        "run": relative(run), "run_id": version["run_id"],
        "git_commit": manifest["git_commit"], "status": manifest["status"],
        "duration_seconds": manifest["duration"],
        "paper_sha256": manifest["paper_sha256"],
        "started_at": version["started_at"], "materials_as_of": version["materials_as_of"],
        "version_sha256": digest(workspace / INDEX_NAME),
        "raw_cards": len(evidence.get("raw_evidence_cards", [])),
        "accepted_cards": len(evidence.get("accepted_evidence_cards", [])),
        "review_count": len(read(workspace / "novelty-reviews.json").get("reviews", [])),
        "conclusion_count": len(read(workspace / "report.json").get("conclusions", [])),
        "conclusion_fields_match_reviews": True,
        "review_statuses": {key: item["status"] for key, item in reviews.items()},
        "limitations": read(workspace / "report.json").get("limitations", []),
        "llm_usage": summary["llm_usage"],
        "provider_requests": summary["provider_requests"],
        "stages": [{key: row.get(key) for key in ("stage_name", "status", "duration_ms")}
                   for row in summary["stages"]],
        "registered_inputs": {name: entry["sha256"] for name, entry in version["artifacts"].items()
                              if entry is not None},
    }


def archive_inputs(workspace, target):
    """Archive every registered compiler input byte, without runtime logs or re-signing."""
    index = read(workspace / INDEX_NAME)
    names = [INDEX_NAME, *(name for name, entry in index["artifacts"].items() if entry)]
    if (workspace / "report/acceptance.md").is_file():
        names.append("report/acceptance.md")
    with tarfile.open(target, "w:gz") as archive:
        for name in sorted(names):
            archive.add(workspace / name, arcname=f"{workspace.name}/{name}", recursive=False)
    return {"path": relative(target), "sha256": digest(target), "file_count": len(names),
            "scope": "All registered Renderer input files and generated acceptance report. Full live runtime and downloaded reference binaries remain in source run directories."}


def validate(args):
    a, source_a = source_details(args.run_a.resolve())
    b, source_b = source_details(args.run_b.resolve())
    assert a.name == b.name
    assert source_a["paper_sha256"] == source_b["paper_sha256"]
    assert source_a["run_id"] != source_b["run_id"]
    assert not args.cases_root.exists(), "Cases directory must be new; do not overwrite evidence."
    cases = {}
    result = {"sources": {"A": source_a, "B": source_b}, "cases": cases}
    for label, source in (("A", a), ("B", b)):
        target = args.cases_root.resolve() / label / source.name
        shutil.copytree(source, target)
        index_digest = digest(target / INDEX_NAME)
        first = subprocess_compile(target)
        assert first["status"] == "rendered" and not first["unfilled_placeholders"], first
        assert not first["missing_attachment_links"] and first["attachment_count"] > 0
        first_text = (ROOT / first["report"]).read_text(encoding="utf-8")
        time.sleep(1.1)
        second = subprocess_compile(target)
        second_text = (ROOT / second["report"]).read_text(encoding="utf-8")
        clean = lambda text: [line for line in text.splitlines() if not line.startswith("| 报告生成时间")]
        assert clean(first_text) == clean(second_text)
        assert first["times"][0] != second["times"][0]
        assert first["times"][1] == second["times"][1]
        assert digest(target / INDEX_NAME) == index_digest
        load_verified_report_inputs(target)
        cases[label] = {"workspace": relative(target), "first": first, "rerender": second,
                        "business_content_stable": True, "material_time_stable": True,
                        "generation_time_changed": True, "input_versions_unchanged": True}

    mixed = args.cases_root.resolve() / "mixed" / a.name
    shutil.copytree(a, mixed)
    # Use real changed phase files from B; retain A's original run/version record.
    candidates = ("evidence-cards.json", "novelty-reviews.json", "retrieval-plans.json", "report.json")
    replacements = []
    for name in candidates:
        if digest(a / name) != digest(b / name):
            shutil.copy2(b / name, mixed / name)
            replacements.append({"path": name, "expected_a_sha256": digest(a / name),
                                 "actual_b_sha256": digest(b / name)})
    assert replacements, "The two real runs must have different phase content."
    rejection = subprocess_compile(mixed)
    assert rejection["status"] == "rejected", rejection
    assert "版本不一致" in rejection["error"]
    assert rejection["report_created"] is False
    assert digest(mixed / INDEX_NAME) == source_a["version_sha256"]
    cases["mixed"] = {"workspace": relative(mixed), "base": "A", "replacements_from": "B",
                      "replacements": replacements, "result": rejection,
                      "version_record_not_rewritten": True}
    args.result.parent.mkdir(parents=True, exist_ok=True)
    for label, case in cases.items():
        case["archive"] = archive_inputs(ROOT / case["workspace"], args.result.parent / f"materials-{label}.tar.gz")
    result["status"] = "PASS"
    args.result.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": "PASS", "result": relative(args.result),
                      "cases": {key: "rejected" if key == "mixed" else "rendered" for key in cases}},
                     ensure_ascii=False))


if __name__ == "__main__":
    def deny_network(event, args):
        if event in {"socket.connect", "socket.getaddrinfo", "socket.sendto"}:
            raise RuntimeError("Network is forbidden during offline acceptance")
    sys.addaudithook(deny_network)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--compile", type=Path)
    parser.add_argument("--run-a", type=Path)
    parser.add_argument("--run-b", type=Path)
    parser.add_argument("--cases-root", type=Path)
    parser.add_argument("--result", type=Path)
    options = parser.parse_args()
    if options.compile:
        sys.exit(compile_one(options.compile.resolve()))
    for key in ("run_a", "run_b", "cases_root", "result"):
        if getattr(options, key) is None:
            parser.error(f"--{key.replace('_', '-')} is required")
    validate(options)
