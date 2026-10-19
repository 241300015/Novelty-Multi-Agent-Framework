#!/usr/bin/env python3
"""Run the minimal A/B/C arXiv smoke without starting a workflow."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "backend" / "src"))

from novelty_agent_framework.experiments.arxiv_rate_smoke import run_smoke
from novelty_agent_framework.tools.database_search.providers.arxiv import ArxivSearchTool


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--min-interval", type=float, default=4.0)
    result.add_argument("--timeout", type=float, default=20.0)
    result.add_argument("--max-retries", type=int, default=1)
    result.add_argument("--max-retry-delay", type=float, default=5.0)
    result.add_argument("--retry-budget", type=float, default=45.0)
    return result


def main() -> int:
    args = parser().parse_args()
    tool = ArxivSearchTool(
        min_interval=args.min_interval,
        timeout=args.timeout,
        max_retries=args.max_retries,
        max_retry_delay=args.max_retry_delay,
        retry_budget_seconds=args.retry_budget,
    )
    result = run_smoke(tool=tool)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["provider_access"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
