"""Live Reader + ToolCallHarness smoke experiment.

Run from the repository root with the project environment configured:

    python -m novelty_agent_framework.experiments.reader_harness_smoke

The script never reads an Artifact directly and never persists credentials.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from backend.env import ChatMessage, ModelCallOptions, ModelClient, ModelResponse

from ..config.factory import build_model_registry, load_config
from ..core import ToolCallHarness, ToolCallHarnessConfig
from ..persistence import ReferenceStore
from ..schemas import NoveltyPoint, ReferenceReadRequest, ResearchTask, TaskResearchRequest
from ..tools import ReaderTool, ReferenceArtifactReaderTool, ResearcherToolRegistry
from ._support import minimal_search_plan

SUBJECT_PAPER_ID = "MF2033k6lC"
ARTIFACT_ID = "art_1b4f8b3b8cb082d6ef83ed76"
OUTPUT_ROOT = Path("outputs")
EXPERIMENT_OUTPUT_DIR = OUTPUT_ROOT / "experiments" / "reader-harness-smoke"
READ_CHAR_START = 0
READ_MAX_CHARS = 3_000

SYSTEM_PROMPT = """你是一个工具调用测试 Agent。

你可以使用提供给你的工具。当用户要求读取指定 Artifact 时，必须使用 reader
工具获取实际内容，不得根据 Artifact ID 猜测内容。获得工具结果后，根据实际读取
到的文本回答用户。只需要简要概括文本主要讨论的内容。"""

USER_TASK = f"""请读取 Artifact `{ARTIFACT_ID}` 从字符 {READ_CHAR_START} 开始的前
{READ_MAX_CHARS} 个字符，并简要说明这段文献主要讨论了什么。"""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class MeasuredModelClient:
    """Record safe latency, usage, and trajectory metadata for each model turn."""

    delegate: ModelClient
    calls: list[dict[str, Any]] = field(default_factory=list)

    def complete(self, messages, *, options=None) -> ModelResponse:
        return self.delegate.complete(messages, options=options)

    async def acomplete(
        self,
        messages: Sequence[ChatMessage],
        *,
        options: ModelCallOptions | None = None,
    ) -> ModelResponse:
        started_at = _utc_now()
        started = time.perf_counter()
        response = await self.delegate.acomplete(messages, options=options)
        duration_ms = round((time.perf_counter() - started) * 1_000, 3)
        self.calls.append(
            {
                "turn": len(self.calls) + 1,
                "started_at": started_at,
                "finished_at": _utc_now(),
                "duration_ms": duration_ms,
                "context_roles": [message.role for message in messages],
                "assistant_tool_call_ids": [
                    call.id
                    for message in messages
                    if message.role == "assistant"
                    for call in message.tool_calls
                ],
                "tool_result_call_ids": [
                    message.tool_call_id
                    for message in messages
                    if message.role == "tool"
                ],
                "response_tool_calls": [
                    {
                        "id": call.id,
                        "name": call.name,
                        "arguments": dict(call.arguments),
                    }
                    for call in response.tool_calls
                ],
                "usage": dict(response.usage),
            }
        )
        return response


def _scope() -> TaskResearchRequest:
    return TaskResearchRequest(
        subject_paper_id=SUBJECT_PAPER_ID,
        run_id="reader-harness-live-smoke",
        novelty_point=NoveltyPoint(
            point_id="NP-reader-smoke",
            claim="验证 Reader 原生工具调用链路",
            technical_features=["Reader Tool Calling"],
        ),
        research_task=ResearchTask(
            task_id="TASK-reader-smoke",
            novelty_point_id="NP-reader-smoke",
            task_type="reader_smoke",
            language="zh",
            description="读取指定 Artifact 并概括文本内容",
        ),
        search_plan=minimal_search_plan("TASK-reader-smoke", "NP-reader-smoke"),
    )


def _token_totals(calls: list[dict[str, Any]]) -> dict[str, int]:
    totals = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    for call in calls:
        usage = call["usage"]
        for key in totals:
            value = usage.get(key)
            if isinstance(value, int):
                totals[key] += value
    return totals


def _trace_record(sequence: int, event) -> dict[str, Any]:
    record: dict[str, Any] = {
        "sequence": sequence,
        "event_type": event.kind,
        "detail": event.detail,
    }
    if event.message is not None:
        record.update(
            {
                "role": event.message.role,
                "content": event.message.content,
                "tool_call_id": event.message.tool_call_id,
                "tool_calls": [
                    {
                        "id": call.id,
                        "name": call.name,
                        "arguments": dict(call.arguments),
                    }
                    for call in event.message.tool_calls
                ],
            }
        )
    if event.tool_call is not None:
        record.update(
            {
                "tool_call_id": event.tool_call.id,
                "tool_name": event.tool_call.name,
                "arguments": dict(event.tool_call.arguments),
            }
        )
    if event.observation is not None:
        record.update(
            {
                "succeeded": event.observation.succeeded,
                "observation": event.observation.model_dump(mode="json"),
                "tool_elapsed_ms": event.observation.elapsed_ms,
            }
        )
    return {key: value for key, value in record.items() if value is not None}


def _write_outputs(result, measured: MeasuredModelClient, summary: dict[str, Any]) -> None:
    EXPERIMENT_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    trace_path = EXPERIMENT_OUTPUT_DIR / "trace.jsonl"
    trace_records = [
        _trace_record(sequence, event)
        for sequence, event in enumerate(result.trace, start=1)
    ]
    trace_path.write_text(
        "".join(
            json.dumps(record, ensure_ascii=False) + "\n" for record in trace_records
        ),
        encoding="utf-8",
    )
    (EXPERIMENT_OUTPUT_DIR / "result.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (EXPERIMENT_OUTPUT_DIR / "final.txt").write_text(
        (result.final_content or "") + "\n",
        encoding="utf-8",
    )
    (EXPERIMENT_OUTPUT_DIR / "model_calls.json").write_text(
        json.dumps(measured.calls, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


async def run_experiment() -> dict[str, Any]:
    if not os.getenv("SILICONFLOW_API_KEY"):
        raise RuntimeError("SILICONFLOW_API_KEY is not configured")

    config = load_config()
    model_alias = config["agents"]["research"]["model"]
    model_registry = build_model_registry(config)
    live_client = model_registry.client_for(model_alias)
    measured_client = MeasuredModelClient(live_client)
    model_name = live_client.profile.model

    store = ReferenceStore(output_root=OUTPUT_ROOT)
    reference_reader = ReferenceArtifactReaderTool(store)
    preflight_started = time.perf_counter()
    preflight = await reference_reader.ainvoke(
        ReferenceReadRequest(
            subject_paper_id=SUBJECT_PAPER_ID,
            artifact_id=ARTIFACT_ID,
            char_start=READ_CHAR_START,
            max_chars=READ_MAX_CHARS,
        )
    )
    preflight_ms = round((time.perf_counter() - preflight_started) * 1_000, 3)

    registry = ResearcherToolRegistry([ReaderTool(reference_reader)])
    harness = ToolCallHarness(
        measured_client,
        registry,
        config=ToolCallHarnessConfig(max_turns=3, max_tool_calls=1),
    )
    experiment_started_at = _utc_now()
    experiment_started = time.perf_counter()
    result = await harness.run(
        system_prompt=SYSTEM_PROMPT,
        initial_user_message=USER_TASK,
        scope=_scope(),
        options=ModelCallOptions(
            temperature=0.0,
            max_tokens=1_024,
            tool_choice="auto",
        ),
    )
    total_duration_ms = round((time.perf_counter() - experiment_started) * 1_000, 3)

    tool_events = [event for event in result.trace if event.kind == "tool_result"]
    reader_events = [
        event
        for event in tool_events
        if event.tool_call is not None and event.tool_call.name == "reader"
    ]
    reader_called = bool(reader_events)
    reader_succeeded = bool(
        reader_events
        and reader_events[0].observation is not None
        and reader_events[0].observation.succeeded
    )
    actual_arguments = (
        dict(reader_events[0].tool_call.arguments) if reader_called else None
    )
    second_context_valid = bool(
        len(measured_client.calls) >= 2
        and measured_client.calls[1]["context_roles"]
        == ["system", "user", "assistant", "tool"]
        and measured_client.calls[1]["assistant_tool_call_ids"]
        == measured_client.calls[1]["tool_result_call_ids"]
    )
    summary = {
        "experiment": "reader-harness-smoke",
        "started_at": experiment_started_at,
        "finished_at": _utc_now(),
        "subject_paper_id": SUBJECT_PAPER_ID,
        "artifact_id": ARTIFACT_ID,
        "model_alias": model_alias,
        "model": model_name,
        "completed": bool(result.final_content),
        "tool_calls_used": result.tool_calls_used,
        "turns_used": result.turns_used,
        "reader_called": reader_called,
        "reader_succeeded": reader_succeeded,
        "reader_arguments": actual_arguments,
        "tool_call_id_matched": second_context_valid,
        "standard_second_turn_context": second_context_valid,
        "final_content_present": bool(result.final_content),
        "preflight": {
            "succeeded": True,
            "duration_ms": preflight_ms,
            "read_id": preflight.read_id,
            "work_id": preflight.work_id,
            "artifact_id": preflight.artifact_id,
            "char_start": preflight.char_start,
            "char_end": preflight.char_end,
        },
        "timing": {
            "total_duration_ms": total_duration_ms,
            "model_duration_ms": round(
                sum(call["duration_ms"] for call in measured_client.calls), 3
            ),
            "reader_tool_duration_ms": (
                reader_events[0].observation.elapsed_ms
                if reader_events and reader_events[0].observation is not None
                else None
            ),
            "model_turns": [
                {
                    "turn": call["turn"],
                    "started_at": call["started_at"],
                    "finished_at": call["finished_at"],
                    "duration_ms": call["duration_ms"],
                }
                for call in measured_client.calls
            ],
        },
        "tokens": {
            "totals": _token_totals(measured_client.calls),
            "per_turn": [
                {"turn": call["turn"], "usage": call["usage"]}
                for call in measured_client.calls
            ],
        },
    }
    _write_outputs(result, measured_client, summary)
    return summary


def main() -> None:
    summary = asyncio.run(run_experiment())
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"outputs: {EXPERIMENT_OUTPUT_DIR}")


if __name__ == "__main__":
    main()
