#!/usr/bin/env python3
"""Read-only CLI for per-run Reader failure diagnostics.

By default every historical run is inspected separately. Use ``--run-id`` to
select one run; results from different runs are never merged into one diagnosis.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "backend" / "src"))

from novelty_agent_framework.diagnostics import (  # noqa: E402
    RuntimeDiagnosticContext,
    inspect_reader_failures,
)


def inspect_run(workspace: Path, run_dir: Path) -> dict:
    return inspect_reader_failures(
        RuntimeDiagnosticContext(
            paper_id=workspace.name,
            run_id=run_dir.name,
            workspace=workspace,
            run_dir=run_dir,
            terminal_status="UNKNOWN",
        )
    )


def inspect_workspace(workspace: Path, *, run_id: str | None = None) -> dict:
    runtime_root = workspace / "runtime"
    if run_id is not None:
        run_dir = runtime_root / run_id
        return inspect_run(workspace, run_dir)
    run_dirs = sorted(path for path in runtime_root.glob("*") if path.is_dir())
    runs = [inspect_run(workspace, run_dir) for run_dir in run_dirs]
    return {
        "workspace": str(workspace),
        "ok": all(item["status"] != "ERROR" for item in runs),
        "runs": runs,
        "warnings": ([] if runs else ["no runtime runs found; enable runtime_debug"]),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("workspace", type=Path, help="outputs/<paper_id> 目录")
    parser.add_argument("--run-id", help="只检查指定 run；默认逐个检查全部 run")
    parser.add_argument("--compact", action="store_true", help="输出单行 JSON")
    args = parser.parse_args()
    report = inspect_workspace(args.workspace, run_id=args.run_id)
    print(json.dumps(report, ensure_ascii=False, indent=None if args.compact else 2, sort_keys=True))
    return 1 if report.get("status") == "ERROR" or report.get("ok") is False else 0


if __name__ == "__main__":
    raise SystemExit(main())
