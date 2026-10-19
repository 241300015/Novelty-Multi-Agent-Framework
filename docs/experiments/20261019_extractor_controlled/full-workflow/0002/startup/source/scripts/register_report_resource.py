"""Register one verified local report bundle without starting a workflow."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from novelty_agent_framework.services.report_resources import ReportResourceStore


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", required=True, type=Path)
    parser.add_argument("--registry", required=True, type=Path)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    resource = ReportResourceStore(args.registry).register(args.bundle, dry_run=args.dry_run)
    print(json.dumps(resource, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
