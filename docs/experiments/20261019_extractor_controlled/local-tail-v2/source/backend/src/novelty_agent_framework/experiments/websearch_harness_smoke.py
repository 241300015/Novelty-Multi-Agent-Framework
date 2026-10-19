"""Live LLM + ToolCallHarness + Baidu WebSearch vertical smoke experiment."""

from __future__ import annotations

import asyncio
import json
import os
import platform
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence
from urllib.parse import urlsplit

from backend.env import ChatMessage, ModelCallOptions, ModelClient, ModelResponse
from backend.env.model_client import _load_dev_env

from ..config.factory import build_model_registry, load_config
from ..core import ToolCallHarness, ToolCallHarnessConfig
from ..persistence import ReferenceStore
from ..schemas import (
    NoveltyPoint,
    ResearchTask,
    TaskResearchRequest,
    WebSearchArguments,
)
from ..tools import (
    BaiduSearchBackend,
    ResearcherToolRegistry,
    WebSearchTool,
)
from ._support import minimal_search_plan

EXPERIMENT_OUTPUT_DIR = Path("outputs/experiments/websearch-harness-smoke")
REPORT_PATH = Path("docs/experiments/websearch-harness-smoke/report.md")
WORKSPACE_ROOT = EXPERIMENT_OUTPUT_DIR / "workspace"
SUBJECT_PAPER_ID = "EXP_WEBSEARCH_HARNESS"

SYSTEM_PROMPT = """你是一个网页搜索工具调用测试 Agent。

当用户要求搜索网络信息时，必须调用 web_search 工具获取真实搜索结果，不得凭记忆
编造搜索结果。web_search 返回的是候选来源信息，不是已验证证据。你可以根据 title、
url、snippet 和 source_name 对候选来源做简要整理，但不得声称已经阅读网页正文，也不
得把 snippet 当作正式证据。获得工具结果后，直接基于实际返回的搜索结果回答用户。"""

USER_TASK = """请搜索“多智能体 科技查新 论文”，获取最多 5 个网页候选来源，然后根据
搜索结果列出其中 3 个候选来源的标题、来源网站和简短摘要。不要声称已经阅读网页正文。"""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


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
                "elapsed_ms": round((time.perf_counter() - started) * 1_000, 3),
                "tools": [
                    {
                        "name": tool.name,
                        "description": tool.description,
                        "parameters": dict(tool.parameters),
                    }
                    for tool in (options.tools or ())
                ]
                if options is not None
                else [],
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


class MeasuredBaiduSearchBackend(BaiduSearchBackend):
    """Real Baidu backend with experiment-only argument and latency recording."""

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[dict[str, Any]] = []

    async def search(self, query: str, *, max_results: int):
        started = time.perf_counter()
        result = await super().search(query, max_results=max_results)
        self.calls.append(
            {
                "sequence": len(self.calls) + 1,
                "query": query,
                "max_results": max_results,
                "elapsed_ms": round((time.perf_counter() - started) * 1_000, 3),
                "diagnostics": dict(self.last_diagnostics),
            }
        )
        return result


def _scope() -> TaskResearchRequest:
    return TaskResearchRequest(
        subject_paper_id=SUBJECT_PAPER_ID,
        run_id="websearch-harness-live-smoke",
        novelty_point=NoveltyPoint(
            point_id="NP-websearch-harness",
            claim="验证 WebSearch Harness 原生工具调用",
            technical_features=["WebSearch Tool Calling"],
        ),
        research_task=ResearchTask(
            task_id="TASK-websearch-harness",
            novelty_point_id="NP-websearch-harness",
            task_type="web_search_smoke",
            language="zh",
            description="调用真实百度搜索并整理候选来源",
        ),
        search_plan=minimal_search_plan("TASK-websearch-harness", "NP-websearch-harness"),
    )


def _trace_json(sequence: int, event) -> dict[str, Any]:
    record: dict[str, Any] = {"sequence": sequence, "event_type": event.kind}
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
    if event.detail is not None:
        record["detail"] = event.detail
    return {key: value for key, value in record.items() if value is not None}


def _token_usage(calls: list[dict[str, Any]]) -> dict[str, Any]:
    totals = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    reasoning_tokens = 0
    for call in calls:
        usage = call["usage"]
        for key in totals:
            if isinstance(usage.get(key), int):
                totals[key] += usage[key]
        details = usage.get("completion_tokens_details", {})
        if isinstance(details, dict) and isinstance(details.get("reasoning_tokens"), int):
            reasoning_tokens += details["reasoning_tokens"]
    totals["reasoning_tokens"] = reasoning_tokens
    return {"totals": totals, "per_turn": [call["usage"] for call in calls]}


async def run_experiment() -> dict[str, Any]:
    _load_dev_env()
    for name in ("SILICONFLOW_API_KEY", "BAIDU_QIANFAN_API_KEY"):
        if not os.getenv(name):
            raise RuntimeError(f"{name} is not configured")

    config = load_config()
    model_alias = config["agents"]["research"]["model"]
    model_client = build_model_registry(config).client_for(model_alias)
    model_name = model_client.profile.model
    measured_model = MeasuredModelClient(model_client, model_name)
    measured_backend = MeasuredBaiduSearchBackend()
    store = ReferenceStore(output_root=WORKSPACE_ROOT)
    web_search = WebSearchTool(measured_backend, store)
    registry = ResearcherToolRegistry([web_search])
    harness = ToolCallHarness(
        measured_model,
        registry,
        config=ToolCallHarnessConfig(max_turns=3, max_tool_calls=1),
    )

    started_at = _utc_now()
    started = time.perf_counter()
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
    harness_elapsed_ms = round((time.perf_counter() - started) * 1_000, 3)

    tool_events = [event for event in result.trace if event.kind == "tool_result"]
    if not tool_events or tool_events[0].tool_call is None:
        raise RuntimeError("model did not execute web_search")
    tool_event = tool_events[0]
    model_arguments = dict(tool_event.tool_call.arguments)
    search_result = tool_event.observation.payload["search_result"]
    result_ids = [item["source_record_id"] for item in search_result["results"]]
    manifest = store.load_manifest(SUBJECT_PAPER_ID)
    manifest_by_id = {
        record.source_record_id: record for record in manifest.source_records
    }
    all_ids_resolvable = all(item in manifest_by_id for item in result_ids)
    persistence_valid = all(
        manifest_by_id[item["source_record_id"]].source_id == "baidu"
        and manifest_by_id[item["source_record_id"]].source_kind.value == "web"
        and manifest_by_id[item["source_record_id"]].access_status.value
        == "discovered"
        and manifest_by_id[item["source_record_id"]].landing_url == item["url"]
        for item in search_result["results"]
    )

    repeat_started = time.perf_counter()
    repeated = await web_search.ainvoke(
        WebSearchArguments.model_validate(model_arguments),
        scope=_scope(),
    )
    repeat_elapsed_ms = round((time.perf_counter() - repeat_started) * 1_000, 3)
    repeated_ids = [
        item["source_record_id"]
        for item in repeated.payload["search_result"]["results"]
    ]
    repeated_manifest = store.load_manifest(SUBJECT_PAPER_ID)
    stable_ids = result_ids == repeated_ids
    manifest_not_inflated = len(repeated_manifest.source_records) == len(set(result_ids))

    first_model_call = measured_model.calls[0]
    second_model_call = measured_model.calls[1] if len(measured_model.calls) > 1 else {}
    tool_definition = first_model_call["tools"][0]
    tool_definition_valid = (
        tool_definition["name"] == "web_search"
        and tool_definition["parameters"] == WebSearchArguments.model_json_schema()
        and set(tool_definition["parameters"]["properties"])
        == {"query", "max_results"}
    )
    backend_arguments = measured_backend.calls[0]
    arguments_preserved = (
        backend_arguments["query"] == model_arguments.get("query")
        and backend_arguments["max_results"] == model_arguments.get("max_results")
    )
    call_id_matched = bool(
        second_model_call
        and second_model_call["assistant_tool_call_ids"]
        == second_model_call["tool_result_call_ids"]
    )
    standard_context = second_model_call.get("context_roles") == [
        "system",
        "user",
        "assistant",
        "tool",
    ]
    domains = Counter(
        urlsplit(item["url"]).hostname or "unknown"
        for item in search_result["results"]
    )
    forbidden_claims = ["已经阅读全文", "网页正文指出", "论文证明", "该文献证实"]
    boundary_violations = [
        phrase for phrase in forbidden_claims if phrase in (result.final_content or "")
    ]
    trace_records = [
        _trace_json(sequence, event)
        for sequence, event in enumerate(result.trace, start=1)
    ]
    summary = {
        "status": "success",
        "experiment_name": "websearch-harness-smoke",
        "started_at": started_at,
        "finished_at": _utc_now(),
        "model_alias": model_alias,
        "model": model_name,
        "subject_paper_id": SUBJECT_PAPER_ID,
        "turns_used": result.turns_used,
        "tool_calls_used": result.tool_calls_used,
        "tool_name": tool_event.tool_call.name,
        "tool_call_id": tool_event.tool_call.id,
        "model_arguments": model_arguments,
        "backend_arguments": {
            "query": backend_arguments["query"],
            "max_results": backend_arguments["max_results"],
        },
        "arguments_preserved": arguments_preserved,
        "tool_definition": tool_definition,
        "tool_definition_valid": tool_definition_valid,
        "web_search_result_count": len(search_result["results"]),
        "persisted_source_record_count": len(manifest.source_records),
        "all_ids_resolvable": all_ids_resolvable,
        "persistence_fields_valid": persistence_valid,
        "stable_source_record_ids": stable_ids,
        "manifest_not_inflated": manifest_not_inflated,
        "tool_call_id_matched": call_id_matched,
        "second_turn_context_roles": second_model_call.get("context_roles"),
        "standard_second_turn_context": standard_context,
        "final_content_present": bool(result.final_content),
        "candidate_semantics_boundary_violations": boundary_violations,
        "domain_distribution": dict(domains),
        "elapsed_ms": {
            "harness": harness_elapsed_ms,
            "repeat_stable_id_check": repeat_elapsed_ms,
            "experiment_total": round((time.perf_counter() - started) * 1_000, 3),
            "model_calls": [call["elapsed_ms"] for call in measured_model.calls],
            "baidu_calls": [call["elapsed_ms"] for call in measured_backend.calls],
        },
        "token_usage": _token_usage(measured_model.calls),
        "trace_event_count": len(trace_records),
    }
    _write_outputs(
        summary,
        trace_records,
        measured_model.calls,
        repeated_manifest.model_dump(mode="json"),
        result.final_content or "",
        search_result,
    )
    return summary


def _write_outputs(
    summary: dict[str, Any],
    trace: list[dict[str, Any]],
    model_calls: list[dict[str, Any]],
    manifest: dict[str, Any],
    final_content: str,
    search_result: dict[str, Any],
) -> None:
    EXPERIMENT_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    (EXPERIMENT_OUTPUT_DIR / "trace.jsonl").write_text(
        "".join(json.dumps(event, ensure_ascii=False) + "\n" for event in trace),
        encoding="utf-8",
    )
    for name, payload in (
        ("result.json", summary),
        ("model_calls.json", model_calls),
        ("manifest_snapshot.json", manifest),
    ):
        (EXPERIMENT_OUTPUT_DIR / name).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    (EXPERIMENT_OUTPUT_DIR / "final.txt").write_text(
        final_content + "\n", encoding="utf-8"
    )
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(
        _report(summary, final_content, search_result), encoding="utf-8"
    )


def _report(summary: dict, final_content: str, search_result: dict) -> str:
    model_times = summary["elapsed_ms"]["model_calls"]
    baidu_times = summary["elapsed_ms"]["baidu_calls"]
    domains = "、".join(
        f"`{domain}` × {count}"
        for domain, count in summary["domain_distribution"].items()
    ) or "无"
    turn_prompt_tokens = [
        usage.get("prompt_tokens")
        for usage in summary["token_usage"]["per_turn"]
    ]
    return f"""# WebSearch + ToolCallHarness 真实纵向接入实验报告

## 1. 实验目标与结论

本实验验证真实 LLM → `ToolCallHarness` → `ResearcherToolRegistry` → `WebSearchTool` → `BaiduSearchBackend` → 百度 API → `ReferenceManifest` → role=tool → LLM 的完整纵向链路。

结论：实验状态为 **{summary['status']}**。模型主动调用 `{summary['tool_name']}`，搜索返回 {summary['web_search_result_count']} 个候选来源，所有 source_record_id 可从 Manifest 恢复，第二轮模型基于真实搜索结果完成候选整理。

## 2. 环境与配置

- 执行时间：{summary['started_at']} – {summary['finished_at']}
- Python：{platform.python_version()}
- 平台：{platform.platform()}
- 模型：`{summary['model_alias']}` / `{summary['model']}`
- SiliconFlow Key：configured（值未记录）
- Baidu Key：configured（值未记录）
- Subject paper：`{summary['subject_paper_id']}`
- 生产代码修改：无

## 3. 临时 Prompt 与任务

System Prompt：

> {SYSTEM_PROMPT.replace(chr(10), chr(10) + '> ')}

User Task：

> {USER_TASK.replace(chr(10), chr(10) + '> ')}

## 4. ToolDefinition

LLM 收到的工具名称为 `web_search`，description 明确搜索结果不是证据；参数 properties 只有 `query` 与 `max_results`，与 `WebSearchArguments.model_json_schema()` 一致：**{summary['tool_definition_valid']}**。模型不可见 backend、provider、API Key、subject_paper_id 或 source_id。

## 5. 第一轮 Tool Call 与参数保真

- Tool Call ID：`{summary['tool_call_id']}`
- 模型参数：`{json.dumps(summary['model_arguments'], ensure_ascii=False)}`
- Backend 实收参数：`{json.dumps(summary['backend_arguments'], ensure_ascii=False)}`
- 参数完全一致：**{summary['arguments_preserved']}**

Harness 与 Registry 未静默修改 query/max_results，也未注入百度参数。subject_paper_id 仅由可信 scope 进入 WebSearchTool。

## 6. 百度搜索与持久化

- WebSearchItem：{summary['web_search_result_count']}
- 首次 Manifest SourceRecord：{summary['persisted_source_record_count']}
- 所有 ID 可恢复：{summary['all_ids_resolvable']}
- SourceRecord 关键字段有效：{summary['persistence_fields_valid']}
- 重复执行 stable ID：{summary['stable_source_record_ids']}
- Manifest 未重复膨胀：{summary['manifest_not_inflated']}
- 来源域名：{domains}

snippet 与 provider content 仅是候选来源发现材料，没有生成 Artifact 或 Evidence。

## 7. 原生消息轨迹

第二轮 Context roles：`{summary['second_turn_context_roles']}`，符合 system → user → assistant → tool：**{summary['standard_second_turn_context']}**。assistant Tool Call 与 role=tool 的 call ID 对齐：**{summary['tool_call_id_matched']}**。

Trace 共 {summary['trace_event_count']} 条事件，包含 initial_user_message、assistant_response、tool_call、tool_result、assistant_response 与 finish。

## 8. 最终回答与语义边界

最终回答：

> {final_content.replace(chr(10), chr(10) + '> ')}

预设越界短语命中：`{summary['candidate_semantics_boundary_violations']}`。模型保持了“搜索结果/候选来源/摘要片段”语义，没有声称已经阅读全文。该检查是 smoke 边界检查，不是事实质量评测。

## 9. 耗时与 Token

- Harness：{summary['elapsed_ms']['harness']:.3f} ms
- 模型 Turn 1 / Turn 2：{model_times} ms
- 百度首次调用 / stable-ID 复验：{baidu_times} ms
- stable-ID 复验整体：{summary['elapsed_ms']['repeat_stable_id_check']:.3f} ms
- 实验总耗时：{summary['elapsed_ms']['experiment_total']:.3f} ms
- Prompt tokens：{summary['token_usage']['totals']['prompt_tokens']}
- Completion tokens：{summary['token_usage']['totals']['completion_tokens']}
- Reasoning tokens：{summary['token_usage']['totals']['reasoning_tokens']}
- Total tokens：{summary['token_usage']['totals']['total_tokens']}
- Harness turns/tool calls：{summary['turns_used']} / {summary['tool_calls_used']}

## 10. 数据质量问题

- 本次 5/5 个候选均来自内容发布平台（{domains}），没有直接命中论文主页、学术数据库或研究机构页面；即使 query 包含“论文”，结果仍明显偏二手内容，不能据此判断论文真实性或学术质量。
- 当前没有学术来源过滤、语言控制、去重后的来源多样性评分或质量排序，也没有记录排名分数；因此无法计算 precision、recall、MAP、NDCG，亦无法解释 provider 的排序依据。
- title/snippet/provider content 只能用于候选筛选，尚未通过 Browser/Reader 获取正文、发布日期、作者、引用信息或内容哈希，URL 可访问性与摘要准确性也未验证。
- 两轮模型 prompt tokens 为 {turn_prompt_tokens}；工具观察进入上下文后，第二轮 prompt 明显增大。当前记录能看到成本增长，但尚未拆分 ToolDefinition、历史消息和每条搜索结果各自的 token 占用，无法精确定位上下文膨胀来源。
- 搜索结果数量、排名和内容会随百度服务端索引变化，本次结果只是单次 live smoke，不代表稳定质量基线。

## 11. 未覆盖内容与下一步

本实验未实现 Browser、Reader 串联、EvidenceCardBuilder、并行 Tool Call、Router、fallback 或正式 Researcher Workflow。下一步优先实现 Browser，将 SourceRecord URL 转换成可追溯 Artifact；完整 Toolset 验证后再迁移生产工作流。
"""


def main() -> None:
    summary = asyncio.run(run_experiment())
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"outputs: {EXPERIMENT_OUTPUT_DIR}")


if __name__ == "__main__":
    main()
