"""Validate and freeze experiment conditions without making network requests."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "backend" / "src"))

from novelty_agent_framework.config import load_application_config
from novelty_agent_framework.config.experiment import freeze_config, preflight_config


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", type=Path)
    parser.add_argument("--isolated-config", action="store_true")
    parser.add_argument("--entrypoint", choices=("paper_input", "full_workflow", "single_task", "web_pdf"), default="paper_input")
    parser.add_argument("--paper-json", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    config = load_application_config(profile_path=args.profile,
                                     environ={} if args.isolated_config else None)
    result = freeze_config(config, entrypoint=args.entrypoint, input_path=args.paper_json)
    result["preflight"] = preflight_config(config, require_reviewer=args.entrypoint != "single_task",
                                            active_roles=("researcher",) if args.entrypoint == "single_task" else None,
                                            include_processing=args.entrypoint in {"web_pdf", "full_workflow"})
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    counts = {level: sum(i["severity"] == level for i in result["preflight"]) for level in ("error", "warning")}
    print(json.dumps({"manifest": str(args.output), "effective_config_sha256": result["effective_config_sha256"], **counts}))
    return int(counts["error"] > 0)


if __name__ == "__main__":
    raise SystemExit(main())
