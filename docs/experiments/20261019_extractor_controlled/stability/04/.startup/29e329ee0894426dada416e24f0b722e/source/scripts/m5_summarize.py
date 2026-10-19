"""M5 汇总：解析 v3 实验产物，输出 v1/v2 对比表。

用法：python scripts/m5_summarize.py [产物目录]
默认产物目录：outputs/experiments/mf2033k6lc-v3-full-workflow
"""

from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict
from pathlib import Path


PROJ = Path(__file__).resolve().parent.parent
OUT = Path(sys.argv[1]) if len(sys.argv) > 1 else PROJ / "outputs" / "experiments" / "mf2033k6lc-v3-full-workflow"
TRACE_DIR = OUT / "traces-coordinator-rerun"
REPORT = PROJ / "outputs" / "experiments" / "m5-comparison.md"

# v1 基线：docs/issues/第一次原型机实验测试结果.md（2026-08-09，两轮检索+补检）
V1_BASELINE = {
    "rounds": 2,
    "research_tasks": 8,
    "executed_queries": 15,
    "model_calls": 19,
    "raw_evidence_cards": 11,
    "accepted_evidence_cards": 6,
    "rejected_evidence_cards": 5,
    "coverage": {"NP-1": "partial", "NP-2": "insufficient", "NP-3": "insufficient"},
    "tokens": 239075,
    "wall_seconds": 1076.0,
}


def load_json(rel: str):
    path = OUT / rel
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def collect_search_executions() -> list[dict]:
    """从 traces 中收集 structured_source_retrieval 的 SearchExecution。"""

    executions: list[dict] = []
    if not TRACE_DIR.exists():
        return executions
    for trace_file in sorted(TRACE_DIR.rglob("*.jsonl")):
        for line in trace_file.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            observation = event.get("full_observation") or {}
            if observation.get("tool_name") not in ("structured_source_retrieval", "database_search"):
                continue
            if not observation.get("succeeded"):
                continue
            payload = observation.get("payload") or {}
            executions = payload.get("search_executions") or []
            if not executions:
                bundle = payload.get("research_bundle") or payload.get("bundle")
                if isinstance(bundle, dict):
                    executions = bundle.get("search_executions") or []
            for execution in executions:
                parameters = execution.get("parameters") or {}
                executions.append({
                    "task_key": trace_file.stem,
                    "strategy_id": parameters.get("strategy_id") or execution.get("query", "")[:20],
                    "variant_id": parameters.get("variant_id"),
                    "base_strategy": parameters.get("base_strategy"),
                    "fallback_reason": parameters.get("fallback_reason"),
                    "query": execution.get("query", ""),
                    "status": execution.get("status"),
                    "hit_count": len(execution.get("results") or []),
                })
    return executions


def main() -> None:
    if not OUT.exists():
        print(f"产物目录不存在：{OUT}")
        raise SystemExit(2)

    executions = collect_search_executions()
    raw_summary = load_json("v3_raw_summary.json") or {}
    workflow_result = load_json("workflow_result.json") or {}
    cards = load_json("evidence_cards_snapshot.json") or []
    raw_cards = load_json("evidence_snapshot.json") or []

    by_status = Counter(item["status"] for item in executions)
    fallback_used = [
        item for item in executions
        if item["variant_id"] and item["variant_id"] != item["base_strategy"]
    ]
    zero_hit = [item for item in executions if item["hit_count"] == 0]

    coverage: dict[str, str] = {}
    for conclusion in (workflow_result.get("conclusions") or []):
        coverage[conclusion["novelty_point_id"]] = conclusion["level"]

    tokens = (raw_summary.get("tokens") or {}).get("total")
    v2 = {
        "rounds": workflow_result.get("rounds", "?"),
        "executed_queries": len(executions),
        "query_status": dict(by_status),
        "zero_hit_queries": len(zero_hit),
        "fallback_variants_used": len(fallback_used),
        "raw_evidence_cards": len(raw_cards),
        "accepted_evidence_cards": len(cards),
        "coverage": coverage,
        "tokens": tokens,
    }

    lines_out = [
        "# M5：v1 vs v2 检索方案对比（时间标注见 m5-v2-tracking.md）",
        "",
        f"实验时间：{OUT.stat().st_mtime}",
        "",
        "## 指标对比",
        "",
        "| 指标 | v1（2026-08-09 基线） | v2（本次） | 说明 |",
        "| --- | --- | --- | --- |",
        f"| 工作流轮数 | {V1_BASELINE['rounds']} | {v2['rounds']} | v3 脚本固定 max_rounds=1 |",
        f"| 执行查询数 | {V1_BASELINE['executed_queries']} | {v2['executed_queries']} | 含放宽变体 |",
        f"| 零命中查询 | - | {v2['zero_hit_queries']} | 新增指标 |",
        f"| 放宽变体使用 | - | {v2['fallback_variants_used']} | 新增指标（M3） |",
        f"| 原始证据卡 | {V1_BASELINE['raw_evidence_cards']} | {v2['raw_evidence_cards']} | |",
        f"| 接受证据卡 | {V1_BASELINE['accepted_evidence_cards']} | {v2['accepted_evidence_cards']} | |",
        f"| 模型调用 | {V1_BASELINE['model_calls']} | {len(raw_summary.get('tasks') or [])}* | *v3 只汇总任务级 |",
        f"| Token 总量 | {V1_BASELINE['tokens']} | {v2['tokens'] or 'n/a'} | |",
        f"| NP-1 | {V1_BASELINE['coverage']['NP-1']} | {coverage.get('NP-1', 'n/a')} | |",
        f"| NP-2 | {V1_BASELINE['coverage']['NP-2']} | {coverage.get('NP-2', 'n/a')} | |",
        f"| NP-3 | {V1_BASELINE['coverage']['NP-3']} | {coverage.get('NP-3', 'n/a')} | |",
        "",
        "## v2 查询明细（来自 traces）",
        "",
        "| 任务 | strategy_id | variant | 状态 | 命中数 | fallback_reason |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for item in sorted(executions, key=lambda x: (x["task_key"], x["strategy_id"] or "")):
        lines_out.append(
            f"| {item['task_key']} | {item['strategy_id']} | {item['variant_id'] or ''} | "
            f"{item['status']} | {item['hit_count']} | {item['fallback_reason'] or ''} |"
        )
    lines_out.extend(["", "---", f"生成时间：{Path(__file__).stat().st_mtime}"])
    REPORT.write_text("\n".join(lines_out) + "\n", encoding="utf-8")
    print(json.dumps(v2, ensure_ascii=False, indent=2))
    print(f"对比表已写入：{REPORT}")


if __name__ == "__main__":
    main()