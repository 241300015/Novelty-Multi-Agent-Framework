"""临时停止策略：Database → Reader → Finish → EvidenceCard → Reviewer。"""

from __future__ import annotations

import asyncio
import argparse
import json
import subprocess
import time
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from backend.env import ModelCallOptions
from backend.env.model_client import _load_dev_env

from ..agents import EvidenceReviewerConfig, NoveltyEvidenceReviewer
from ..config import (
    build_model_registry,
    build_prompt_library,
    build_workflow,
    effective_safe_config,
    load_application_config,
)
from ..schemas import (
    NoveltyPoint,
    PaperInput,
    EvidenceCard,
    ResearchFinishDraft,
    SearchConcept,
    SearchPlan,
    SearchStrategy,
    TaskResearchRequest,
)
from ..tools import ResearcherToolRegistry
from .evidence_card_builder_live_smoke import _trace
from .issue7_single_task_database_only_live import FailingLegacyPlanner
from .mf2033k6lc_v3_researcher_attempt2 import MeasuredClient, TASK_KEY, token_total

OUTPUT = Path("outputs/experiments/database-reader-finish-evidence-reviewer-live")
PAPER_INPUT = Path("outputs/MF2033k6lC/paper-input/others/paper.json")
POINTS_INPUT = Path("outputs/MF2033k6lC/novelty-points.json")
SELECTED_POINT_ID = "NP-1"
SELECTED_TASK_ID = "T-1"
PROMPT_NAME = "research/database_reader_finish_once"


class ConfiguredReviewerClient:
    """让实验 Reviewer 实际使用 reviewer.json 中的超时与 token 上限。"""

    def __init__(self, delegate, model_config) -> None:
        self.delegate = delegate
        self.model_config = model_config

    @property
    def profile(self):
        return self.delegate.profile

    def complete(self, messages, *, options=None):
        effective = replace(
            options or ModelCallOptions(),
            max_tokens=self.model_config.max_tokens,
            timeout_seconds=self.model_config.timeout_seconds,
        )
        return self.delegate.complete(messages, options=effective)


def write_json(name: str, value: Any) -> None:
    OUTPUT.mkdir(parents=True, exist_ok=True)
    (OUTPUT / name).write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def review_payload(result) -> dict[str, Any]:
    return {
        "accepted": [card.model_dump(mode="json") for card in result.accepted],
        "rejected": [list(item) for item in result.rejected],
        "needs_more": list(result.needs_more),
        "decisions": [item.model_dump(mode="json") for item in result.decisions],
    }


def controlled_search_plan(task) -> SearchPlan:
    """使用前序 live 已验证能在 arXiv 召回候选的控制计划。"""

    return SearchPlan(
        task_id=task.task_id,
        novelty_point_id=task.novelty_point_id,
        concepts=[
            SearchConcept(
                concept_id="C1",
                name="图摘要技术",
                terms=["图摘要", "图摘要技术", "Graph summarization"],
            ),
            SearchConcept(
                concept_id="C2",
                name="图自编码器",
                terms=["图自编码器", "Graph autoencoder"],
            ),
            SearchConcept(
                concept_id="C3",
                name="循环神经网络",
                terms=["循环神经网络", "RNN", "Recurrent neural network"],
            ),
            SearchConcept(
                concept_id="C4",
                name="动态时序图建模",
                terms=["动态图", "Dynamic graph", "Dynamic sequential graph modeling"],
            ),
        ],
        strategies=[
            SearchStrategy(
                strategy_id="S1",
                level="strict",
                expression="C1 AND C2 AND C3 AND C4",
                description="完整组合",
            ),
            SearchStrategy(
                strategy_id="S2",
                level="medium",
                expression="C1 AND C2 AND C3",
                description="保留核心架构",
            ),
            SearchStrategy(
                strategy_id="S3",
                level="broad",
                expression="(C1 OR C2) AND (C3 OR C4)",
                description="已验证的宽检索组合",
            ),
        ],
    )


async def run() -> dict[str, Any]:
    _load_dev_env()
    config = load_application_config()
    prompts = build_prompt_library()
    registry = build_model_registry(config)
    paper = PaperInput.model_validate_json(PAPER_INPUT.read_text(encoding="utf-8"))
    point_payload = json.loads(POINTS_INPUT.read_text(encoding="utf-8"))
    point = next(
        NoveltyPoint.model_validate(item)
        for item in point_payload["novelty_points"]
        if item["point_id"] == SELECTED_POINT_ID
    )

    built = build_workflow(config)
    brief = built.services.coordinator.plan(paper, points=[point], attempt=1)
    task = next(
        item
        for item in brief.research_tasks
        if item.novelty_point_id == SELECTED_POINT_ID
        and item.task_id == SELECTED_TASK_ID
    )

    plan = controlled_search_plan(task)
    request = TaskResearchRequest(
        subject_paper_id=paper.paper_id,
        run_id="database-reader-finish-evidence-reviewer-live",
        novelty_point=point,
        research_task=task,
        search_plan=plan,
    )

    researcher = built.services.task_researcher
    database = researcher.tools.get("database_search")
    reader = researcher.tools.get("reader")
    restricted_tools = ResearcherToolRegistry([database, reader])
    researcher.tools = restricted_tools
    researcher.harness.registry = restricted_tools
    researcher.config = replace(researcher.config, prompt_name=PROMPT_NAME)

    legacy_guards: list[FailingLegacyPlanner] = []
    for retrieval in database.tools_by_source.values():
        guard = FailingLegacyPlanner()
        retrieval.search_planner = guard
        legacy_guards.append(guard)

    researcher_client = MeasuredClient(researcher.model_client, "researcher")
    researcher.model_client = researcher_client
    researcher.harness.model_client = researcher_client
    captured_trace = ()
    harness_result = None
    original_run = researcher.harness.run

    async def recording_harness(**kwargs: Any):
        nonlocal captured_trace, harness_result
        try:
            harness_result = await original_run(**kwargs)
            captured_trace = harness_result.trace
            return harness_result
        except Exception as exc:
            captured_trace = tuple(getattr(exc, "trace", ()))
            raise

    researcher.harness.run = recording_harness
    started_at = datetime.now(timezone.utc).isoformat()
    started = time.perf_counter()
    token = TASK_KEY.set(f"{SELECTED_POINT_ID}/{SELECTED_TASK_ID}")
    try:
        task_result = await researcher.ainvoke(request)
    finally:
        TASK_KEY.reset(token)
    researcher_elapsed_ms = round((time.perf_counter() - started) * 1000, 3)

    reviewer_cfg = config.reviewer
    reviewer_alias = (
        reviewer_cfg.model.alias if reviewer_cfg is not None else config.researcher.model.alias
    )
    reviewer_model_config = (
        reviewer_cfg.model if reviewer_cfg is not None else config.researcher.model
    )
    reviewer_client = MeasuredClient(
        ConfiguredReviewerClient(
            registry.client_for(reviewer_alias), reviewer_model_config
        ),
        "reviewer",
    )
    reviewer = NoveltyEvidenceReviewer(
        model_client=reviewer_client,
        prompts=prompts,
        config=EvidenceReviewerConfig(
            enabled=True,
            model_alias=reviewer_alias,
            temperature=(reviewer_cfg.model.temperature if reviewer_cfg else 0.0),
            max_cards_per_call=(reviewer_cfg.max_cards_per_call if reviewer_cfg else 8),
            fail_closed=(reviewer_cfg.fail_closed if reviewer_cfg else True),
        ),
    )
    review_started = time.perf_counter()
    review_result = reviewer.review(
        task_result.evidence_cards,
        points=[point],
        tasks=[task],
    )
    reviewer_elapsed_ms = round((time.perf_counter() - review_started) * 1000, 3)

    tool_calls = [
        event.tool_call
        for event in captured_trace
        if event.kind == "tool_call" and event.tool_call is not None
    ]
    tool_names = [call.name for call in tool_calls]
    successful_results = [
        event.observation
        for event in captured_trace
        if event.kind == "tool_result"
        and event.observation is not None
        and event.observation.succeeded
    ]
    finish_draft = None
    finish_error = None
    if harness_result is not None:
        try:
            finish_draft = ResearchFinishDraft.model_validate_json(
                harness_result.final_content or ""
            )
        except Exception as exc:
            finish_error = f"{type(exc).__name__}: {exc}"

    cards = list(task_result.evidence_cards)
    summary = {
        "experiment": "Database Reader Finish Evidence Reviewer Live",
        "started_at": started_at,
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "git_sha": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], text=True
        ).strip(),
        "temporary_prompt": PROMPT_NAME,
        "search_plan_mode": "controlled_plan_from_prior_successful_live_retrieval",
        "selected_task": {
            "point_id": point.point_id,
            "task_id": task.task_id,
            "task_type": task.task_type,
            "language": task.language,
        },
        "registered_tools": list(restricted_tools.names),
        "disabled_tools": ["web_search", "browser"],
        "tool_sequence": tool_names,
        "tool_counts": {
            name: tool_names.count(name)
            for name in ("database_search", "reader", "web_search", "browser")
        },
        "successful_tool_results": [item.tool_name for item in successful_results],
        "database_then_reader_then_finish": (
            tool_names == ["database_search", "reader"] and harness_result is not None
        ),
        "finish_reached": harness_result is not None,
        "finish_draft_valid": finish_draft is not None,
        "finish_error": finish_error,
        "finish_card_count": len(finish_draft.cards) if finish_draft else 0,
        "finish_no_evidence_reason": (
            finish_draft.no_evidence_reason if finish_draft else None
        ),
        "task_status": task_result.status.value,
        "task_warnings": list(task_result.warnings),
        "read_count": len(task_result.read_results),
        "evidence_count": len(task_result.evidence),
        "evidence_card_count": len(cards),
        "evidence_cards_generated": bool(cards),
        "reviewer_called": bool(cards),
        "reviewer_model_alias": reviewer_alias,
        "review": {
            "accepted_count": len(review_result.accepted),
            "rejected_count": len(review_result.rejected),
            "needs_more_count": len(review_result.needs_more),
            "decision_count": len(review_result.decisions),
            "result": review_payload(review_result),
        },
        "legacy_planner_calls": sum(guard.calls for guard in legacy_guards),
        "elapsed_ms": {
            "researcher": researcher_elapsed_ms,
            "reviewer": reviewer_elapsed_ms,
        },
        "tokens": {
            "search_planner": 0,
            "researcher": token_total(researcher_client.calls),
            "reviewer": token_total(reviewer_client.calls),
            "total": token_total(
                researcher_client.calls + reviewer_client.calls
            ),
        },
    }

    OUTPUT.mkdir(parents=True, exist_ok=True)
    (OUTPUT / "trace.jsonl").write_text(
        "".join(
            json.dumps(_trace(index, event), ensure_ascii=False) + "\n"
            for index, event in enumerate(captured_trace, 1)
        ),
        encoding="utf-8",
    )
    write_json("summary.json", summary)
    write_json("task_request.json", request.model_dump(mode="json"))
    write_json("task_result.json", task_result.model_dump(mode="json"))
    write_json("evidence_cards.json", [card.model_dump(mode="json") for card in cards])
    write_json("review_result.json", review_payload(review_result))
    write_json("effective_config.json", effective_safe_config(config))
    write_json(
        "model_calls.json",
        {
            "search_planner": [],
            "researcher": researcher_client.calls,
            "reviewer": reviewer_client.calls,
        },
    )
    return summary


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--review-only", action="store_true")
    args = parser.parse_args()
    result = review_existing() if args.review_only else asyncio.run(run())
    print(json.dumps(result, ensure_ascii=False, indent=2))


def review_existing() -> dict[str, Any]:
    """复用已生成 EvidenceCard，仅恢复正式 Reviewer 调用。"""

    _load_dev_env()
    config = load_application_config()
    reviewer_cfg = config.reviewer
    reviewer_alias = (
        reviewer_cfg.model.alias if reviewer_cfg is not None else config.researcher.model.alias
    )
    reviewer_model_config = (
        reviewer_cfg.model if reviewer_cfg is not None else config.researcher.model
    )
    client = MeasuredClient(
        ConfiguredReviewerClient(
            build_model_registry(config).client_for(reviewer_alias),
            reviewer_model_config,
        ),
        "reviewer",
    )
    reviewer = NoveltyEvidenceReviewer(
        model_client=client,
        prompts=build_prompt_library(),
        config=EvidenceReviewerConfig(
            enabled=True,
            model_alias=reviewer_alias,
            temperature=reviewer_model_config.temperature,
            max_cards_per_call=(reviewer_cfg.max_cards_per_call if reviewer_cfg else 8),
            fail_closed=(reviewer_cfg.fail_closed if reviewer_cfg else True),
        ),
    )
    cards = [
        EvidenceCard.model_validate(item)
        for item in json.loads((OUTPUT / "evidence_cards.json").read_text(encoding="utf-8"))
    ]
    request = TaskResearchRequest.model_validate_json(
        (OUTPUT / "task_request.json").read_text(encoding="utf-8")
    )
    started = time.perf_counter()
    result = reviewer.review(
        cards,
        points=[request.novelty_point],
        tasks=[request.research_task],
    )
    elapsed_ms = round((time.perf_counter() - started) * 1000, 3)
    payload = review_payload(result)
    write_json("review_result.json", payload)
    write_json("reviewer_resume_model_calls.json", client.calls)
    summary = json.loads((OUTPUT / "summary.json").read_text(encoding="utf-8"))
    summary["review"] = {
        "accepted_count": len(result.accepted),
        "rejected_count": len(result.rejected),
        "needs_more_count": len(result.needs_more),
        "decision_count": len(result.decisions),
        "result": payload,
    }
    summary["reviewer_called"] = True
    summary["reviewer_resume"] = {
        "used": True,
        "timeout_seconds": reviewer_model_config.timeout_seconds,
        "max_tokens": reviewer_model_config.max_tokens,
        "elapsed_ms": elapsed_ms,
        "tokens": token_total(client.calls),
    }
    summary["tokens"]["reviewer"] = token_total(client.calls)
    summary["tokens"]["total"] += token_total(client.calls)
    write_json("summary.json", summary)
    return summary


if __name__ == "__main__":
    main()
