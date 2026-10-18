"""Issue #20 live acceptance against the configured local vLLM endpoint."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from backend.env import (
    ModelCallOptions,
    ModelProfile,
    OpenAICompatibleChatClient,
    PromptLibrary,
)
from novelty_agent_framework.agents import NoveltyPointExtractorAgent, build_paper_digest
from novelty_agent_framework.core.paper_reading import PaperReader
from novelty_agent_framework.core.runtime_artifacts import (
    RuntimeArtifactManager,
    RuntimeDebugConfig,
)
from novelty_agent_framework.persistence import persist_workflow_input
from novelty_agent_framework.schemas import PaperInput


pytestmark = pytest.mark.live

PROMPTS_ROOT = Path("backend/src/novelty_agent_framework/prompts")
LATE_MARKER = "HELIOS-LATE"


def _chunk(heading: str, sentence: str) -> str:
    value = heading + "\n"
    while len(value) < 4000:
        value += sentence
    return value[:4000]


@pytest.mark.skipif(
    not os.getenv("LOCAL_VLLM_API_KEY"),
    reason="需要 LOCAL_VLLM_API_KEY 和可访问的本地 vLLM tunnel",
)
def test_live_persisted_reader_extracts_late_contribution(tmp_path) -> None:
    """A real model must read the persisted tail, then cite an issued location."""

    first = _chunk(
        "INTRODUCTION — background only",
        " This passage describes established graph scheduling background only.",
    )
    second = _chunk(
        "RELATED WORK — no author contribution",
        " This passage compares conventional static schedulers and baselines.",
    )
    third = (
        "LATE METHODS — PRIMARY ORIGINAL CONTRIBUTION\n"
        f"We propose {LATE_MARKER}, a hierarchical event-triggered graph scheduler. "
        "It combines uncertainty-gated queue partitioning with a rollback-safe "
        "temporal consistency rule. These two coupled mechanisms are the paper's "
        "independent technical contribution.\n"
        "Experiments and limitations follow."
    )
    paper = PaperInput(
        paper_id="issue20-live-late-contribution",
        title="Adaptive Scheduling for Dynamic Graph Systems",
        abstract="This paper studies scheduling for dynamic graph systems.",
        full_text=first + second + third,
        keywords_en=["dynamic graph", "scheduler"],
    )

    artifact_root = Path(
        os.getenv("ISSUE20_LIVE_ARTIFACT_DIR", str(tmp_path / "live-artifacts"))
    ).resolve()
    artifact_root.mkdir(parents=True, exist_ok=True)
    manager = RuntimeArtifactManager(
        paper.paper_id,
        config=RuntimeDebugConfig(
            output_root=artifact_root / "outputs",
            archive_root=artifact_root / "archive",
            max_model_calls=16,
            llm_pricing_path=artifact_root / "local-vllm-unpriced.json",
        ),
        model_provider="openai_compatible",
        model_name="qwen2.5-7b-instruct",
        stage_names=("extract_points",),
    )
    manager.activate()
    started = time.monotonic()
    try:
        workspace, reference = persist_workflow_input(
            paper,
            output_root=artifact_root / "outputs",
            with_reading_reference=True,
        )
        persisted, _ = PaperReader(reference).load()
        digest = build_paper_digest(persisted).model_copy(
            update={"paper_source": reference}
        )
        assert LATE_MARKER not in digest.full_text_excerpt

        profile = ModelProfile(
            alias="local-qwen2.5-7b",
            model="qwen2.5-7b-instruct",
            base_url="http://127.0.0.1:8000/v1",
            api_key=os.getenv("LOCAL_VLLM_API_KEY"),
            context_window=32768,
            defaults={"temperature": 0.0, "timeout_seconds": 180.0},
        )
        agent = NoveltyPointExtractorAgent(
            model_client=OpenAICompatibleChatClient(profile),
            prompts=PromptLibrary(PROMPTS_ROOT),
            model_options=ModelCallOptions(
                temperature=0.0,
                max_tokens=2048,
                timeout_seconds=180.0,
            ),
        )
        points = list(agent.extract(digest, previous_brief=None, attempt=1))

        late_reads = [
            item
            for item in agent.last_trace["reading"]["reads"]
            if LATE_MARKER in item["text"]
        ]
        assert late_reads, "真实模型没有读取摘要截断范围之外的贡献段"
        allowed_locations = {
            item["source_location"]
            for item in agent.last_trace["reading"]["reads"]
        }
        allowed_locations.update(
            item["source_location"]
            for item in agent._reading_context["metadata"]
        )
        assert points
        assert all(point.source_locations for point in points)
        assert all(
            location in allowed_locations
            for point in points
            for location in point.source_locations
        )
        assert any(
            LATE_MARKER.lower()
            in " ".join(
                [
                    point.claim,
                    point.claim_en,
                    *point.technical_features,
                    *point.technical_features_en,
                ]
            ).lower()
            for point in points
        ), "真实模型读取了后段，但最终查新点没有保留该独立贡献"

        result = {
            "status": "SUCCESS",
            "model": profile.model,
            "endpoint": profile.base_url,
            "duration_seconds": round(time.monotonic() - started, 3),
            "paper_characters": len(paper.full_text),
            "digest_contains_late_marker": LATE_MARKER in digest.full_text_excerpt,
            "read_ranges": [
                {
                    key: item[key]
                    for key in ("segment_id", "start", "end", "source_location")
                }
                for item in agent.last_trace["reading"]["reads"]
            ],
            "unread_segments": agent.last_trace["reading"]["unread_segments"],
            "coverage_complete": agent.last_trace["coverage_complete"],
            "point_count": len(points),
            "points": [point.model_dump(mode="json") for point in points],
            "model_calls_used": agent.last_trace["model_calls_used"],
            "reading_model_calls_used": agent.last_trace["reading"][
                "model_calls_used"
            ],
            "runtime_run_id": manager.run_id,
            "workspace": str(workspace),
        }
        (artifact_root / "live-result.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        (artifact_root / "point-extraction-trace.json").write_text(
            json.dumps(agent.last_trace, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        manager.finish_run("SUCCESS")
    except BaseException as exc:
        failure = {
            "status": "FAILED",
            "error_type": type(exc).__name__,
            "error": str(exc),
            "duration_seconds": round(time.monotonic() - started, 3),
            "runtime_run_id": manager.run_id,
        }
        (artifact_root / "live-result.json").write_text(
            json.dumps(failure, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        manager.finish_run("FAILED", error=exc)
        raise
    finally:
        manager.deactivate()
