"""M5 最终指标（精简解析，避免内存问题）。
"""

import json
from collections import Counter
from pathlib import Path

OUT = Path("outputs/experiments/mf2033k6lc-v3-full-workflow")
TRACE_DIR = OUT / "traces-coordinator-rerun"

executions = []
for trace_file in sorted(TRACE_DIR.rglob("*.jsonl")):
    task_key = str(trace_file.relative_to(TRACE_DIR)).replace("\\", "/").replace(".jsonl", "")
    for line in trace_file.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except Exception:
            continue
        obs = event.get("full_observation") or {}
        if obs.get("tool_name") not in ("database_search", "structured_source_retrieval"):
            continue
        if not obs.get("succeeded"):
            continue
        payload = obs.get("payload") or {}
        exes = payload.get("search_executions") or []
        for ex in exes:
            params = ex.get("parameters") or {}
            executions.append({
                "task": task_key,
                "strategy_id": params.get("strategy_id", "?"),
                "variant_id": params.get("variant_id", ""),
                "base_strategy": params.get("base_strategy", ""),
                "fallback_reason": params.get("fallback_reason") or "",
                "status": ex.get("status", "?"),
                "hits": len(ex.get("results") or []),
                "query": (ex.get("query") or "")[:70],
            })
        del event, obs, payload, exes

total = len(executions)
by_status = Counter(e["status"] for e in executions)
zero_hit = sum(1 for e in executions if e["hits"] == 0)
fallbacks = [e for e in executions if e["variant_id"] and e["variant_id"] != e["base_strategy"]]
hit_queries = [e for e in executions if e["hits"] > 0]

print(json.dumps({
    "total_executions": total,
    "by_status": dict(by_status),
    "zero_hit": zero_hit,
    "fallback_variants_used": len(fallbacks),
    "queries_with_hits": len(hit_queries),
    "total_hits": sum(e["hits"] for e in executions),
}, ensure_ascii=False, indent=1))
print("--- 每任务查询明细 ---")
for e in executions:
    print(f"{e['task']} | {e['strategy_id']} | {e['variant_id'] or '-'} | {e['status']} | hits={e['hits']} | {e['fallback_reason'][:40]} | {e['query']}")