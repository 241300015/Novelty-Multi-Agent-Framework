import json, sys
from pathlib import Path
OUT = Path("outputs/experiments/mf2033k6lc-v3-full-workflow")
TRACE_DIR = OUT / "traces-coordinator-rerun"
n = 0
for trace_file in sorted(TRACE_DIR.rglob("*.jsonl")):
    for line in trace_file.read_text(encoding="utf-8").splitlines():
        if not line.strip(): continue
        try: event = json.loads(line)
        except Exception: continue
        obs = event.get("full_observation") or {}
        if obs.get("tool_name") != "database_search": continue
        if not obs.get("succeeded"): continue
        payload = obs.get("payload") or {}
        exes = payload.get("search_executions") or []
        n += len(exes)
        if exes:
            print(trace_file, len(exes), exes[0].get("parameters", {}).get("strategy_id"), file=sys.stderr)
print("TOTAL_EXECUTIONS", n)