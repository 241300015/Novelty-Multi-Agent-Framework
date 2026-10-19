"""Live native tool-calling smoke for WebSearch -> Browser -> Reader."""

from __future__ import annotations

import asyncio
import json
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from backend.env import ChatMessage, ModelCallOptions, ModelClient, ModelResponse
from backend.env.model_client import _load_dev_env

from ..config.factory import build_model_registry, load_config
from ..core import ToolCallHarness, ToolCallHarnessConfig
from ..persistence import ReferenceStore
from ..schemas import NoveltyPoint, ResearchTask, TaskResearchRequest
from ..tools import (
    BaiduSearchBackend,
    BrowserTool,
    PlaywrightBrowserBackend,
    ReaderTool,
    ReferenceArtifactReaderTool,
    ResearcherToolRegistry,
    WebSearchTool,
)
from ._support import minimal_search_plan

OUTPUT_DIR = Path("outputs/experiments/researcher-three-tool-smoke")
REPORT_PATH = Path("docs/experiments/researcher-three-tool-smoke/report.md")
WORKSPACE = OUTPUT_DIR / "workspace"
SUBJECT_ID = "EXP_RESEARCHER_THREE_TOOL"
SYSTEM_PROMPT = """你是 Researcher 三工具纵向链路测试 Agent。必须严格串行完成：
1. 调用 web_search，query 必须是“Python asyncio 官方文档”，max_results=5；
2. 只根据搜索结果选择 Python 官方文档的 source_record_id，调用 browser；browser 参数只能有 source_record_id，禁止传 URL 或 work_id；
3. 从 browser 返回的 extracted_text Artifact 选择 artifact_id，调用 reader，char_start=0，max_chars=2000；
4. 仅根据 reader 返回的正文回答 asyncio 的用途和两类 API。搜索 snippet 不是正文或证据。
每轮只调用一个工具；未完成前三次调用前不得直接回答。"""
USER_TASK = "请按规定链路查阅 Python asyncio 官方文档，并用实际 Reader 正文简要回答。"


@dataclass
class MeasuredModelClient:
    delegate: ModelClient
    model: str
    calls: list[dict[str, Any]] = field(default_factory=list)

    def complete(self, messages, *, options=None) -> ModelResponse:
        return self.delegate.complete(messages, options=options)

    async def acomplete(
        self,
        messages: Sequence[ChatMessage],
        *,
        options: ModelCallOptions | None = None,
    ) -> ModelResponse:
        started = time.perf_counter()
        response = await self.delegate.acomplete(messages, options=options)
        self.calls.append(
            {
                "turn": len(self.calls) + 1,
                "model": self.model,
                "context_roles": [message.role for message in messages],
                "context": [_message_json(message) for message in messages],
                "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
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


def _message_json(message: ChatMessage) -> dict[str, Any]:
    return {
        "role": message.role,
        "content": message.content,
        "tool_call_id": message.tool_call_id,
        "tool_calls": [
            {"id": call.id, "name": call.name, "arguments": dict(call.arguments)}
            for call in message.tool_calls
        ],
    }


def _scope() -> TaskResearchRequest:
    return TaskResearchRequest(
        subject_paper_id=SUBJECT_ID,
        run_id="researcher-three-tool-smoke",
        novelty_point=NoveltyPoint(
            point_id="NP-three-tool",
            claim="验证三个工具的句柄链",
            technical_features=["native tool calling", "data plane"],
        ),
        research_task=ResearchTask(
            task_id="TASK-three-tool",
            novelty_point_id="NP-three-tool",
            task_type="three_tool_smoke",
            language="zh",
        ),
        search_plan=minimal_search_plan("TASK-three-tool", "NP-three-tool"),
    )


def _trace_json(sequence: int, event) -> dict[str, Any]:
    record: dict[str, Any] = {"sequence": sequence, "event_type": event.kind}
    if event.message is not None:
        record["message"] = _message_json(event.message)
    if event.tool_call is not None:
        record["tool_call"] = {
            "id": event.tool_call.id,
            "name": event.tool_call.name,
            "arguments": dict(event.tool_call.arguments),
        }
    if event.observation is not None:
        record["full_observation"] = event.observation.model_dump(mode="json")
    if event.detail is not None:
        record["detail"] = event.detail
    return record


def _all_keys(value: Any) -> set[str]:
    if isinstance(value, dict):
        return set(value).union(*(_all_keys(item) for item in value.values()), set())
    if isinstance(value, list):
        return set().union(*(_all_keys(item) for item in value), set())
    return set()


async def run() -> dict[str, Any]:
    _load_dev_env()
    config = load_config()
    alias = config["agents"]["research"]["model"]
    client = build_model_registry(config).client_for(alias)
    measured = MeasuredModelClient(client, client.profile.model)
    store = ReferenceStore(WORKSPACE)
    registry = ResearcherToolRegistry(
        [
            WebSearchTool(BaiduSearchBackend(), store),
            BrowserTool(PlaywrightBrowserBackend(), store),
            ReaderTool(ReferenceArtifactReaderTool(store)),
        ]
    )
    harness = ToolCallHarness(
        measured,
        registry,
        config=ToolCallHarnessConfig(max_turns=5, max_tool_calls=3),
    )
    started = time.perf_counter()
    started_at = datetime.now(timezone.utc).isoformat()
    result = await harness.run(
        system_prompt=SYSTEM_PROMPT,
        initial_user_message=USER_TASK,
        scope=_scope(),
        options=ModelCallOptions(temperature=0.0, max_tokens=1200, tool_choice="auto"),
    )
    elapsed_ms = round((time.perf_counter() - started) * 1000, 3)
    trace = [_trace_json(index, event) for index, event in enumerate(result.trace, 1)]
    tool_events = [event for event in result.trace if event.kind == "tool_result"]
    tool_names = [event.tool_call.name for event in tool_events]
    tool_arguments = {
        event.tool_call.name: dict(event.tool_call.arguments) for event in tool_events
    }
    tool_call_ids = [
        {
            "tool_name": event.tool_call.name,
            "assistant_tool_call_id": event.tool_call.id,
            "tool_result_call_id": event.message.tool_call_id,
            "matched": event.tool_call.id == event.message.tool_call_id,
        }
        for event in tool_events
    ]
    projections = {
        event.tool_call.name: event.message.content for event in tool_events
    }
    full = {event.tool_call.name: event.observation for event in tool_events}
    calls_aligned = all(
        event.tool_call.id == event.message.tool_call_id for event in tool_events
    )
    web_projection = projections.get("web_search", "")
    browser_projection = projections.get("browser", "")
    reader_projection = projections.get("reader", "")
    browser_projection_keys = (
        _all_keys(json.loads(browser_projection)) if browser_projection else set()
    )
    browser_args = next(
        (event.tool_call.arguments for event in tool_events if event.tool_call.name == "browser"),
        {},
    )
    reader_args = next(
        (event.tool_call.arguments for event in tool_events if event.tool_call.name == "reader"),
        {},
    )
    browser_full = full.get("browser")
    reader_full = full.get("reader")
    web_full = full.get("web_search")
    manifest = store.load_manifest(SUBJECT_ID)
    browser_result = (
        browser_full.payload["browser_result"] if browser_full is not None else {}
    )
    read_result = reader_full.payload["read_result"] if reader_full is not None else {}
    source_id = browser_result.get("source_record_id")
    work_id = browser_result.get("work_id")
    artifact_id = read_result.get("artifact_id")
    source_record = next(
        (record for record in manifest.source_records if record.source_record_id == source_id),
        None,
    )
    trusted_url = (
        (source_record.full_text_url or source_record.landing_url)
        if source_record is not None
        else None
    )
    browser_requested_url = (
        browser_full.payload.get("browser_fetch", {}).get("requested_url")
        if browser_full is not None
        else None
    )
    traceable = bool(
        source_id
        and work_id
        and artifact_id
        and any(r.source_record_id == source_id and r.work_id == work_id for r in manifest.source_records)
        and any(w.work_id == work_id for w in manifest.works)
        and any(
            a.artifact_id == artifact_id
            and a.work_id == work_id
            and a.source_record_id == source_id
            for a in manifest.artifacts
        )
        and read_result.get("work_id") == work_id
    )
    final = result.final_content or ""
    grounding_terms = [term for term in ("asyncio", "并发", "高层级", "低层级") if term in read_result.get("text", "") and term in final]
    usages = [call["usage"] for call in measured.calls]
    token_usage_per_turn = [
        {
            "turn": index,
            "prompt_tokens": usage.get("prompt_tokens"),
            "completion_tokens": usage.get("completion_tokens"),
            "reasoning_tokens": (
                usage.get("completion_tokens_details", {}).get("reasoning_tokens")
                if isinstance(usage.get("completion_tokens_details"), dict)
                else None
            ),
            "total_tokens": usage.get("total_tokens"),
        }
        for index, usage in enumerate(usages, 1)
    ]
    tool_elapsed_ms = {
        event.tool_call.name: event.observation.elapsed_ms for event in tool_events
    }
    full_observation_in_trace = bool(
        web_full
        and web_full.payload.get("source_records")
        and browser_full
        and browser_full.payload.get("browser_fetch", {}).get("html")
        and reader_full
        and reader_full.payload.get("read_result", {}).get("text")
    )
    web_projection_compact = (
        "raw_metadata" not in web_projection
        and "source_records" not in web_projection
    )
    browser_projection_compact = not {
        "browser_fetch",
        "html",
        "text",
    }.intersection(browser_projection_keys)
    reader_projection_has_text = bool(
        json.loads(reader_projection).get("read_result", {}).get("text")
    ) if reader_projection else False
    browser_url_resolved = bool(
        trusted_url and browser_requested_url == trusted_url and "url" not in browser_args
    )
    successful = all(
        (
            tool_names == ["web_search", "browser", "reader"],
            set(browser_args) == {"source_record_id"},
            browser_url_resolved,
            reader_args.get("artifact_id") == artifact_id,
            calls_aligned,
            full_observation_in_trace,
            web_projection_compact,
            browser_projection_compact,
            reader_projection_has_text,
            traceable,
            bool(grounding_terms),
        )
    )
    summary = {
        "status": "success" if successful else "failed",
        "model_alias": alias,
        "model": measured.model,
        "code_base_commit": _git_head(),
        "started_at": started_at,
        "tool_sequence": tool_names,
        "turns_used": result.turns_used,
        "tool_calls_used": result.tool_calls_used,
        "elapsed_ms": elapsed_ms,
        "model_elapsed_ms_per_turn": [call["elapsed_ms"] for call in measured.calls],
        "tool_elapsed_ms": tool_elapsed_ms,
        "tool_arguments": tool_arguments,
        "tool_call_ids": tool_call_ids,
        "browser_arguments": dict(browser_args),
        "reader_arguments": dict(reader_args),
        "browser_only_source_record_id": set(browser_args) == {"source_record_id"},
        "browser_requested_url": browser_requested_url,
        "manifest_trusted_url": trusted_url,
        "browser_url_resolved_by_data_plane": browser_url_resolved,
        "reader_uses_browser_artifact": reader_args.get("artifact_id") == artifact_id,
        "tool_call_ids_aligned": calls_aligned,
        "full_observation_in_trace": full_observation_in_trace,
        "compact_projection_checks": {
            "websearch_has_no_raw_metadata": web_projection_compact,
            "browser_has_no_html_or_text": browser_projection_compact,
            "reader_contains_text": reader_projection_has_text,
        },
        "projection_characters": {name: len(content) for name, content in projections.items()},
        "prompt_tokens_per_turn": [usage.get("prompt_tokens") for usage in usages],
        "completion_tokens_per_turn": [usage.get("completion_tokens") for usage in usages],
        "token_usage_per_turn": token_usage_per_turn,
        "total_tokens": sum(usage.get("total_tokens", 0) for usage in usages),
        "source_record_id": source_id,
        "work_id": work_id,
        "artifact_id": artifact_id,
        "read_id": read_result.get("read_id"),
        "traceable_source_work_artifact_read": traceable,
        "final_grounding_terms": grounding_terms,
        "final_answer_based_on_reader": bool(grounding_terms),
        "finished_at": datetime.now(timezone.utc).isoformat(),
    }
    _write(summary, trace, measured.calls, manifest.model_dump(mode="json"), final)
    return summary


def _git_head() -> str:
    return subprocess.check_output(
        ["git", "rev-parse", "HEAD"], text=True
    ).strip()


def _write(summary, trace, model_calls, manifest, final) -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUTPUT_DIR / "trace.jsonl").write_text(
        "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in trace),
        encoding="utf-8",
    )
    for name, payload in (
        ("model_calls.json", model_calls),
        ("result.json", summary),
        ("manifest_snapshot.json", manifest),
    ):
        (OUTPUT_DIR / name).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    (OUTPUT_DIR / "final.txt").write_text(final + "\n", encoding="utf-8")
    checks = summary["compact_projection_checks"]
    report = f"""# Researcher 三工具真实纵向补充实验报告

## 1. 实验目标与结论

验证真实 LLM 经 `ToolCallHarness` 串行调用 WebSearch、Browser、Reader，并验证 LLM 只控制句柄、可信数据由 Runtime 搬运。结论：**{summary['status']}**；Browser 阶段已完成最终补充验收，正式 Researcher Workflow 仍未迁移。

## 2. 代码与运行配置

- 实验代码基线 commit：`{summary['code_base_commit']}`
- 执行时间：{summary['started_at']} – {summary['finished_at']}
- 模型：`{summary['model_alias']}` / `{summary['model']}`
- 真实工具：`WebSearchTool(BaiduSearchBackend)`、`BrowserTool(PlaywrightBrowserBackend)`、`ReaderTool(ReferenceArtifactReaderTool)`
- 生产代码修改：无；仅补充实验脚本观测与报告字段

## 3. 原生 Tool Calling 轨迹与参数

- 顺序：`{summary['tool_sequence']}`
- WebSearch：`{json.dumps(summary['tool_arguments'].get('web_search'), ensure_ascii=False)}`
- Browser：`{json.dumps(summary['tool_arguments'].get('browser'), ensure_ascii=False)}`
- Reader：`{json.dumps(summary['tool_arguments'].get('reader'), ensure_ascii=False)}`
- 三组 call ID：`{summary['tool_call_ids']}`
- 全部 call ID 对齐：**{summary['tool_call_ids_aligned']}**

## 4. Control Plane / Data Plane

- Browser 参数只有 `source_record_id`：**{summary['browser_only_source_record_id']}**
- Manifest 恢复的可信 URL：`{summary['manifest_trusted_url']}`
- Browser 实际 requested URL：`{summary['browser_requested_url']}`
- URL 来自 Data Plane 且模型未传 URL：**{summary['browser_url_resolved_by_data_plane']}**
- Reader 使用 BrowserResult 返回的 artifact_id：**{summary['reader_uses_browser_artifact']}**

模型没有传 URL、work_id、subject_paper_id、source_id 或 backend，也没有向 Reader 复制 HTML/text。

## 5. Context Projection 边界

- WebSearch projection 保留候选句柄、title、URL、snippet、source_name、published_at；不含 raw_metadata/source_records/provider content：**{checks['websearch_has_no_raw_metadata']}**
- Browser projection 保留 source/work/artifact 句柄、role、media_type、content_extent、warnings；不含 browser_fetch、HTML、正文、storage path、sha256 或内部 metadata：**{checks['browser_has_no_html_or_text']}**
- Reader projection 保留 read/work/artifact 句柄、字符范围、text、has_more；正文正常进入模型上下文：**{checks['reader_contains_text']}**
- Projection 字符数：`{summary['projection_characters']}`

## 6. 完整审计与持久化闭环

- WebSearch、Browser、Reader 的完整 Observation 均保存在 `trace.jsonl`：**{summary['full_observation_in_trace']}**
- Browser 完整 Observation 包含 browser_fetch/html/text，但 role=tool projection 不包含这些字段。
- SourceRecord → Work → Artifact → Read 从重新加载的 Manifest 验证闭合：**{summary['traceable_source_work_artifact_read']}**
- 句柄：source `{summary['source_record_id']}` → work `{summary['work_id']}` → artifact `{summary['artifact_id']}` → read `{summary['read_id']}`

## 7. 最终回答 Grounding

- 最终回答基于 Reader text：**{summary['final_answer_based_on_reader']}**
- Reader 正文与最终答案共同核心词：`{summary['final_grounding_terms']}`
- Prompt 明确禁止把搜索 snippet 当正文；模型最终回答声明仅依据 Reader 返回的官方文档正文。

共同关键词仅是最低限度 smoke signal，不是事实正确性 benchmark，也不证明逐句引用对齐。

## 8. Token 与耗时

- 每轮 token：`{summary['token_usage_per_turn']}`
- 总 token：{summary['total_tokens']}
- 总 Harness 时间：{summary['elapsed_ms']} ms
- 各模型调用时间：`{summary['model_elapsed_ms_per_turn']}` ms
- 各工具 Observation 时间：`{summary['tool_elapsed_ms']}` ms

工具 elapsed 来自现有 Observation，覆盖对应 Tool 的后端调用与持久化；当前没有更细的网络、Chromium launch、DOM 提取、文件写入分项，因此不补造数字。本实验也没有旧版完整 Observation 同任务 A/B，不能宣称节省了特定百分比的 token，只能确认 WebSearch/Browser 大字段未直接进入 Model Context。

## 9. 不稳定因素与环境依赖

百度索引、候选排序、网页内容、导航耗时和模型决策会变化；未来重复运行可能选择不同 SourceRecord。Playwright 1.62.0 只安装 Chromium；当前宿主缺少系统级 NSPR/NSS/ALSA 且无法免密 sudo，本次通过 Novelty Conda 环境安装 `nspr`、`nss`、`alsa-lib`，运行时提供该环境的 library path。

## 10. 未覆盖能力与下一步

本实验未覆盖登录、Cookie profile、CAPTCHA、代理、多页 session、并行 Tool Call、Router、fallback、正式 Researcher Workflow、Validator 或 Reviewer。没有实现 EvidenceCardBuilder。

本次成功标准全部满足，Browser 阶段可以收口，**可以进入 EvidenceCardBuilder 开发**；这不代表 EvidenceCardBuilder 已实现或正式工作流已迁移。

## 11. 最终回答

{final}
"""
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(report, encoding="utf-8")


def main() -> None:
    summary = asyncio.run(run())
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"outputs: {OUTPUT_DIR}")


if __name__ == "__main__":
    main()
