"""离线复制已封存材料，在新进程中对照两模板；不启动模型或查询数据库。"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT / "backend/src"), str(ROOT)]
from novelty_agent_framework.report_inputs import INDEX_NAME, load_verified_report_inputs


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, action="append", required=True)
    parser.add_argument("--destination", type=Path, required=True)
    args = parser.parse_args()
    args.destination.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, PYTHONPATH=os.pathsep.join([str(ROOT / "backend/src"), str(ROOT)]))
    results = []
    for number, source in enumerate(args.workspace, 1):
        source = source.resolve()
        index, verified = load_verified_report_inputs(source)
        index_bytes = (source / INDEX_NAME).read_bytes()
        case = args.destination / f"case-{number}"
        workspace = case / source.name
        # Re-running is allowed only on an identical input snapshot.
        for relative, raw in {**verified, INDEX_NAME: index_bytes}.items():
            path = workspace / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.exists() and path.read_bytes() != raw:
                raise ValueError(f"Existing snapshot differs: {path}")
            path.write_bytes(raw)
        paths = {}
        for name in ("default", "concise"):
            destination = case / f"{name}.md"
            command = [sys.executable, "-c", "import sys; from novelty_agent_framework.tools.renderer import render_report; "
                       "render_report(paper_name=sys.argv[1],output_root=sys.argv[2],template_name=sys.argv[3],save_path=sys.argv[4],expected_run_id=sys.argv[5])",
                       source.name, str(case.resolve()), name, str(destination.resolve()), index["run_id"]]
            subprocess.run(command, cwd=case, env=env, check=True, capture_output=True)
            first = destination.read_text()
            subprocess.run(command, cwd=case, env=env, check=True, capture_output=True)
            second = destination.read_text()
            stable = lambda text: [line for line in text.splitlines() if not line.startswith(("| 报告生成时间", "| 编制日期"))]
            assert stable(first) == stable(second)
            assert "{{" not in second
            for target in re.findall(r"\]\(<([^>]+)>\)", second):
                from urllib.parse import unquote
                assert (destination.parent / unquote(target)).is_file(), target
            paths[name] = second
        # The presentation prefers persisted Coordinator prose, with the existing
        # review comparison used only if prose is absent or a failure placeholder.
        report = json.loads(verified["report.json"])
        points = json.loads(verified["novelty-points.json"])["novelty_points"]
        point_numbers = {point["point_id"]: i for i, point in enumerate(points, 1)}
        labels = {"novel": "新颖", "partially_novel": "部分新颖", "not_novel": "不新颖"}
        for conclusion in report.get("conclusions", []):
            if conclusion["review_status"] == "insufficient_evidence":
                label = "证据不足，无法裁定" if conclusion.get("incomplete_reason") not in {
                    "material_unavailable", "technical_error", "budget_exhausted"} else "核验未完成，无法裁定"
            else:
                label = labels[conclusion["verdict"]]
            point_id = conclusion["novelty_point_id"]
            assert f"### {point_id} · {label}" in paths["default"]
            narrative = str(conclusion.get("summary") or "").strip()
            placeholders = {"Reviewer 核验未完成，尚不能作出新颖性裁定。",
                            "该查新点的关键比较证据不足，尚不能作出新颖性裁定。"}
            if narrative and narrative not in placeholders:
                assert f"**查新点 {point_numbers[point_id]}**" in paths["concise"]
                readable = narrative.replace("Reviewer", "评审").replace("Baseline", "对比方法")
                assert readable in paths["concise"]
            else:
                assert f"**查新点 {point_numbers[point_id]}：{label}。**" in paths["concise"]
        assert len(paths["concise"]) < len(paths["default"])
        assert all(word not in paths["concise"] for word in (
            "{{", "Gate A", "Gate B", "置信度", "阶段产物文件目录", "[原文]",
            "本次补充检索上限", "执行问题", "完整性检查", "资源额度已用尽"))
        # Mutation after sealing must fail and must not overwrite either report.
        material = workspace / "evidence-cards.json"
        raw = material.read_bytes()
        material.write_bytes(raw + b"\n")
        blocked = []
        try:
            for name in ("default", "concise"):
                probe = [sys.executable, "-c", command[2], source.name, str(case.resolve()), name,
                         str((case / f"{name}.md").resolve()), index["run_id"]]
                result = subprocess.run(probe, cwd=case, env=env, capture_output=True, text=True)
                assert result.returncode != 0 and "版本不一致" in result.stderr
                assert (case / f"{name}.md").read_text() == paths[name]
                blocked.append(name)
        finally:
            material.write_bytes(raw)
        assert (source / INDEX_NAME).read_bytes() == index_bytes
        assert (workspace / INDEX_NAME).read_bytes() == index_bytes
        _, original = load_verified_report_inputs(source)
        assert original == verified
        results.append({"case": number, "source": str(source.relative_to(ROOT)), "run_id": index["run_id"],
                        "index_sha256": digest(index_bytes), "input_files": len(verified),
                        "conclusions": [{key: c.get(key) for key in ("novelty_point_id", "review_status", "verdict", "incomplete_reason")}
                                        for c in report.get("conclusions", [])],
                        "characters": {name: len(text) for name, text in paths.items()},
                        "reduction_percent": round(100 * (1 - len(paths["concise"]) / len(paths["default"])), 1),
                        "fresh_process_repeat": "passed", "links_and_placeholders": "passed", "tamper_blocked": blocked})
    files = [ROOT / "backend/src/novelty_agent_framework/tools/renderer.py",
             ROOT / "backend/src/novelty_agent_framework/templates/markdown/concise.md", Path(__file__)]
    output = {"network_or_model_calls": 0, "source_sha256": {str(p.relative_to(ROOT)): digest(p.read_bytes()) for p in files}, "cases": results}
    (args.destination / "results.json").write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"cases": len(results), "reductions": [row["reduction_percent"] for row in results]}))


if __name__ == "__main__":
    main()
