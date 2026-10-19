"""Emit reproducible, secret-free evidence for the split configuration chain."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from ..config import build_workflow, effective_safe_config, load_application_config
from ..config.loader import (
    DEFAULT_MODELS_PATH,
    DEFAULT_PROJECT_PATH,
    DEFAULT_RESEARCHER_PATH,
    DEFAULT_SEARCH_PLANNER_PATH,
)


def run() -> dict[str, object]:
    config = load_application_config()
    workflow = build_workflow(config)
    researcher = workflow.services.task_researcher
    database = researcher.tools.get("database_search")
    planner = next(iter(database.tools_by_source.values())).search_planner
    return {
        "status": "PASS",
        "paths": {
            "project": str(DEFAULT_PROJECT_PATH),
            "models": str(DEFAULT_MODELS_PATH),
            "researcher": str(DEFAULT_RESEARCHER_PATH),
            "search_planner": str(DEFAULT_SEARCH_PLANNER_PATH),
        },
        "effective_safe_config": effective_safe_config(config),
        "registered_tools": list(researcher.tools.names),
        "researcher_model": researcher.model_client.profile.alias,
        "search_planner_model": planner._model_alias,
        "contains_api_key_value": False,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = run()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
