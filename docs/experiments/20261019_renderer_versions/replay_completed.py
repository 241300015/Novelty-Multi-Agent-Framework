"""Compile additional completed historical workflows offline, independently."""
from __future__ import annotations

import argparse
from pathlib import Path
import subprocess
import sys
import time

import replay_existing as replay
import validate_materials as matrix
from novelty_agent_framework.tools.renderer import _is_web_card


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="append", required=True, type=Path)
    parser.add_argument("--work-root", required=True, type=Path)
    parser.add_argument("--result", required=True, type=Path)
    args = parser.parse_args()
    assert not args.work_root.exists(), "Use a fresh work root."
    args.result.parent.mkdir(parents=True, exist_ok=True)
    result = {
        "scope": "Independent offline compilation of additional historical full workflows. No new agent execution or model changes; no assertion that different source commits are identical.",
        "test_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
        "model_calls": 0, "network_calls": 0, "cases": {},
    }
    for number, source in enumerate(args.run, start=1):
        label = f"completed-{number:02}"
        audited = replay.audit_source(source.resolve())
        imported = replay.import_snapshot(audited, args.work_root.resolve() / label)
        workspace, details = matrix.source_details(imported)
        before_index = matrix.digest(workspace / matrix.INDEX_NAME)
        first = matrix.subprocess_compile(workspace)
        assert first["status"] == "rendered", first
        first_text = (workspace / "report/acceptance.md").read_text(encoding="utf-8")
        time.sleep(1.1)
        second = matrix.subprocess_compile(workspace)
        assert second["status"] == "rendered", second
        text = (workspace / "report/acceptance.md").read_text(encoding="utf-8")
        for compiled in (first, second):
            assert not compiled["missing_attachment_links"], compiled
            assert not compiled["unfilled_placeholders"], compiled
            assert compiled["attachment_count"] > 0
        stable_lines = lambda value: [line for line in value.splitlines()
                                     if not line.startswith("| 报告生成时间")]
        assert stable_lines(first_text) == stable_lines(text)
        assert first["times"][0] != second["times"][0]
        assert first["times"][1] == second["times"][1]
        assert matrix.digest(workspace / matrix.INDEX_NAME) == before_index
        cards = matrix.read(workspace / "evidence-cards.json")["accepted_evidence_cards"]
        references = matrix.read(workspace / "references/list.json")
        visible_cards = [card for card in cards if not _is_web_card(card, references)]
        for card in visible_cards:
            assert f"### {card['card_id']} · {card['document_title']}" in text, card["card_id"]
        assert f"| 最终 accepted 卡 | {len(cards)} |" in text
        partials = matrix.read(workspace / "report.json").get("partial_card_reviews", [])
        for partial in partials:
            rows = [line for line in text.splitlines()
                    if line.startswith(f"| {partial['card_id']} | 汇总 ")]
            assert len(rows) == 1, partial["card_id"]
            verdict = partial["review"]["verdict"]
            expected_verdict = "—" if verdict is None else str(verdict)
            assert f"| {expected_verdict} |" in rows[0], rows[0]
        for name, expected in audited[3]["original_file_sha256"].items():
            assert matrix.digest(audited[1] / name) == expected, "Source changed"
            assert matrix.digest(workspace / name) == expected, "Imported business content changed"
        result["cases"][label] = {
            "source": details, "audit": audited[3], "first": first, "rerender": second,
            "evidence_cards_rendered": len(visible_cards), "partial_reviews_rendered": len(partials),
            "generation_time_changed": True, "material_time_stable": True,
            "business_content_stable": True, "source_and_imported_files_unchanged": True,
            "index_unchanged": True,
            "archive": matrix.archive_inputs(workspace, args.result.parent / f"{label}.tar.gz"),
        }
    result["status"] = "PASS"
    replay.write(args.result, result)
    print(f"PASS: {len(result['cases'])} additional completed workflows; {args.result}")


if __name__ == "__main__":
    def deny_network(event, args):
        if event in {"socket.connect", "socket.getaddrinfo", "socket.sendto"}:
            raise RuntimeError("Network is forbidden during offline acceptance")
    sys.addaudithook(deny_network)
    main()
