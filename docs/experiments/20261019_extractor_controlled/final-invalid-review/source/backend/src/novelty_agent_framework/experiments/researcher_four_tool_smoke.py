"""Real autonomous TaskResearcher smoke with all four business tools registered."""

from __future__ import annotations

import asyncio
import argparse
import json
import time
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from ..config import effective_safe_config, load_application_config
from ..config.factory import build_workflow
from ..schemas import NoveltyPoint, ResearchTask, TaskResearchRequest
from ._support import minimal_search_plan


class MeasuredClient:
    def __init__(self, inner) -> None:
        self.inner = inner
        self.profile = inner.profile
        self.usages = []

    def complete(self, messages, *, options=None):
        response = self.inner.complete(messages, options=options)
        self.usages.append(dict(response.usage))
        return response

    async def acomplete(self, messages, *, options=None):
        response = await self.inner.acomplete(messages, options=options)
        self.usages.append(dict(response.usage))
        return response


class RequiredAllToolsPrompts:
    """Experiment-only capability instruction; not a strategy-quality test."""

    def __init__(self, inner) -> None:
        self.inner = inner

    def render(self, name, **variables):
        rendered = self.inner.render(name, **variables)
        return replace(rendered, system=rendered.system + "\n" + (
            "Capability experiment only: call database_search(source_id=\"arxiv\") first; "
            "then reader on one returned database artifact; then call web_search once, "
            "browser on one returned Web source, reader on its artifact, and finish. "
            "Do not repeat a discovery tool."
        ))

async def run(*, require_all: bool = False) -> dict[str, object]:
    config = load_application_config()
    workflow = build_workflow(config)
    researcher = workflow.services.task_researcher
    if require_all:
        researcher.prompts = RequiredAllToolsPrompts(researcher.prompts)
    main_client = MeasuredClient(researcher.model_client)
    researcher.model_client = main_client
    researcher.harness.model_client = main_client

    database = researcher.tools.get("database_search")
    planners = {item.search_planner for item in database.tools_by_source.values()}
    planner = next(iter(planners))
    planner_client = MeasuredClient(planner._client())
    planner.model_client = planner_client

    tool_sequence = []
    observations = []
    original_execute = researcher.tools.execute

    async def measured_execute(tool_name, arguments, *, scope):
        tool_sequence.append(tool_name)
        observation = await original_execute(tool_name, arguments, scope=scope)
        observations.append(observation)
        return observation

    researcher.tools.execute = measured_execute
    mode_suffix = "required" if require_all else "autonomous"
    run_stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    request = TaskResearchRequest(
        subject_paper_id=f"researcher-four-tool-live-smoke-{mode_suffix}-{run_stamp}",
        run_id=f"researcher-four-tool-live-smoke-run-{mode_suffix}-{run_stamp}",
        novelty_point=NoveltyPoint(
            point_id="NP-live-four",
            claim="Temporal graph neural networks using graph summarization",
            claim_en="Temporal graph neural networks using graph summarization",
            technical_features=["temporal graph neural network", "graph summarization"],
        ),
        research_task=ResearchTask(
            task_id="T-live-four", novelty_point_id="NP-live-four",
            task_type="search", language="en",
        ),
        search_plan=minimal_search_plan("T-live-four", "NP-live-four"),
    )
    started = time.monotonic()
    result = await researcher.ainvoke(request)
    store = researcher.evidence_builder.reference_store
    manifest = store.load_manifest(request.subject_paper_id)
    warnings = [warning for item in observations for warning in item.payload.get("database_search_result", {}).get("warnings", [])]
    warnings.extend(result.warnings)
    return {
        "registered_tools": list(researcher.tools.names),
        "effective_safe_config": effective_safe_config(config),
        "experiment_mode": "required_all_capability" if require_all else "autonomous",
        "tool_sequence": tool_sequence,
        "per_tool_counts": {name: tool_sequence.count(name) for name in researcher.tools.names},
        "researcher_model": main_client.profile.alias,
        "researcher_calls": len(main_client.usages),
        "researcher_token_usage": main_client.usages,
        "search_planner_model": planner_client.profile.alias,
        "search_planner_calls": len(planner_client.usages),
        "search_planner_token_usage": planner_client.usages,
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "manifest": {
            "works": len(manifest.works),
            "source_records": len(manifest.source_records),
            "artifacts": len(manifest.artifacts),
        },
        "trusted_reads": len(result.read_results),
        "evidence": len(result.evidence),
        "evidence_cards": len(result.evidence_cards),
        "finish_status": result.status.value,
        "research_bundles": len(result.research_bundles),
        "warnings": warnings,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--require-all", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    rendered = json.dumps(
        asyncio.run(run(require_all=args.require_all)), ensure_ascii=False, indent=2
    )
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)


if __name__ == "__main__":
    main()
