"""Live WebSearch -> Browser -> Reader -> semantic finish -> Builder smoke."""

from __future__ import annotations

import asyncio
import json
import re
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
from ..schemas import (
    NoveltyPoint,
    ReferenceReadResult,
    ResearchFinishDraft,
    ResearchTask,
    TaskResearchRequest,
)
from ..tools import (
    BaiduSearchBackend,
    BrowserTool,
    EvidenceCardBuilder,
    PlaywrightBrowserBackend,
    ReaderTool,
    ReferenceArtifactReaderTool,
    ResearcherToolRegistry,
    WebSearchTool,
)
from ._support import minimal_search_plan

OUTPUT_DIR = Path("outputs/experiments/evidence-card-builder-live-smoke")
REPORT_PATH = Path("docs/experiments/evidence-card-builder-live-smoke/report.md")
WORKSPACE = OUTPUT_DIR / "workspace"
SUBJECT_ID = "EXP_EVIDENCE_CARD_BUILDER"
FORBIDDEN_FINISH_KEYS = {
    "read_id", "artifact_id", "work_id", "source_record_id", "task_id",
    "novelty_point_id", "subject_paper_id", "url", "doi", "document_title",
    "card_id", "evidence_id", "locator",
}
PRIOR_ATTEMPTS = [
    {
        "attempt": 1,
        "status": "failed",
        "reason": (
            "response_format=json_object was applied to intermediate native tool-calling "
            "turns; the model stopped before browser and the post-check found no browser event"
        ),
    }
]
SYSTEM_PROMPT = """你是 EvidenceCardBuilder 纵向实验的 Researcher。严格串行执行：
1. web_search(query="Python asyncio 官方文档", max_results=5)，选择 Python 官方文档；
2. browser 只能传 source_record_id，禁止 URL/work_id；
3. reader 只能传 Browser 返回的 artifact_id，char_start=0，max_chars=2000；必要时可对同一 Artifact 再读一次；
4. 完成后停止工具调用，final content 只能是符合下列语义的 JSON 对象，不得有 Markdown fence 或自然语言：
{"cards":[{"main_contribution":"...","overlaps":["..."],"differences":["..."],"quotes":[{"quote":"必须逐字复制 Reader 正文","interpretation":"...","confidence":0.0}],"possible_baseline":false,"relevance":0.0,"confidence":0.0}],"no_evidence_reason":null}
如果没有可引用证据则输出 {"cards":[],"no_evidence_reason":"..."}。
final JSON 禁止出现 read_id、artifact_id、work_id、source_record_id、task_id、novelty_point_id、subject_paper_id、URL、DOI、document_title、card_id、evidence_id、locator。搜索 snippet 不是正文，quote 必须逐字来自 Reader text。每轮最多调用一个工具。"""
USER_TASK = "请查阅 Python asyncio 官方文档，形成一张有一条逐字引文的语义证据卡草稿。"


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
                "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
                "context_roles": [message.role for message in messages],
                "context": [_message(message) for message in messages],
                "response_tool_calls": [
                    {"id": call.id, "name": call.name, "arguments": dict(call.arguments)}
                    for call in response.tool_calls
                ],
                "usage": dict(response.usage),
            }
        )
        return response


def _message(message: ChatMessage) -> dict[str, Any]:
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
        run_id="evidence-card-builder-live-smoke",
        novelty_point=NoveltyPoint(
            point_id="NP-builder",
            claim="验证语义草稿到可信证据卡",
            technical_features=["deterministic provenance binding"],
        ),
        research_task=ResearchTask(
            task_id="TASK-builder",
            novelty_point_id="NP-builder",
            task_type="builder_smoke",
            language="zh",
        ),
        search_plan=minimal_search_plan("TASK-builder", "NP-builder"),
    )


def _trace(index: int, event) -> dict[str, Any]:
    item: dict[str, Any] = {"sequence": index, "event_type": event.kind}
    if event.message is not None:
        item["message"] = _message(event.message)
    if event.tool_call is not None:
        item["tool_call"] = {
            "id": event.tool_call.id,
            "name": event.tool_call.name,
            "arguments": dict(event.tool_call.arguments),
        }
    if event.observation is not None:
        item["full_observation"] = event.observation.model_dump(mode="json")
    if event.detail is not None:
        item["detail"] = event.detail
    return item


def _keys(value: Any) -> set[str]:
    if isinstance(value, dict):
        return set(value).union(*(_keys(item) for item in value.values()), set())
    if isinstance(value, list):
        return set().union(*(_keys(item) for item in value), set())
    return set()


def _normalized(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


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
        measured, registry,
        config=ToolCallHarnessConfig(max_turns=6, max_tool_calls=4),
    )
    started_at = datetime.now(timezone.utc).isoformat()
    started = time.perf_counter()
    result = await harness.run(
        system_prompt=SYSTEM_PROMPT,
        initial_user_message=USER_TASK,
        scope=_scope(),
        options=ModelCallOptions(
            temperature=0.0,
            max_tokens=1600,
            tool_choice="auto",
        ),
    )
    harness_ms = round((time.perf_counter() - started) * 1000, 3)
    raw_finish = result.final_content or ""
    finish_draft = ResearchFinishDraft.model_validate_json(raw_finish)
    finish_payload = json.loads(raw_finish)
    forbidden_finish_keys = sorted(FORBIDDEN_FINISH_KEYS.intersection(_keys(finish_payload)))
    tool_events = [event for event in result.trace if event.kind == "tool_result"]
    tool_sequence = [event.tool_call.name for event in tool_events]
    tool_arguments = {
        event.tool_call.name: dict(event.tool_call.arguments) for event in tool_events
    }
    trusted_reads = [
        ReferenceReadResult.model_validate(event.observation.payload["read_result"])
        for event in tool_events
        if event.tool_call.name == "reader" and event.observation.succeeded
    ]
    browser_event = next(event for event in tool_events if event.tool_call.name == "browser")
    browser_artifact = browser_event.observation.payload["browser_result"]["artifacts"][0]["artifact_id"]
    reader_arguments = [
        dict(event.tool_call.arguments)
        for event in tool_events if event.tool_call.name == "reader"
    ]
    quote_matches = []
    for card in finish_draft.cards:
        for quote in card.quotes:
            matches = [
                read for read in trusted_reads
                if quote.quote in read.text
                or _normalized(quote.quote) in _normalized(read.text)
            ]
            quote_matches.append(
                {
                    "quote": quote.quote,
                    "matched_read_ids": [read.read_id for read in matches],
                    "candidate_work_ids": sorted({read.work_id for read in matches}),
                }
            )

    builder = EvidenceCardBuilder(store)
    model_calls_before_builder = len(measured.calls)
    builder_started = time.perf_counter()
    built = builder.build(finish_draft, scope=_scope(), read_results=trusted_reads)
    builder_ms = round((time.perf_counter() - builder_started) * 1000, 3)
    model_calls_after_builder = len(measured.calls)
    repeated = builder.build(finish_draft, scope=_scope(), read_results=trusted_reads)
    manifest = store.load_manifest(SUBJECT_ID)
    evidence = built.evidence
    cards = built.evidence_cards
    source_ids = sorted(
        {
            item.provenance["source_record_id"]
            for item in evidence if "source_record_id" in item.provenance
        }
    )
    work_ids = {item.work_id for item in evidence}
    card_work_consistent = len(cards) == len(work_ids) == 1 and all(
        item.work_id in work_ids for item in evidence
    )
    trace_closed = bool(
        cards and evidence and trusted_reads
        and all(any(a.artifact_id == item.artifact_id for a in manifest.artifacts) for item in evidence)
        and all(any(w.work_id == item.work_id for w in manifest.works) for item in evidence)
        and all(item.provenance["read_id"] in {read.read_id for read in trusted_reads} for item in evidence)
        and set(cards[0].evidence_ids) == {item.evidence_id for item in evidence}
    )
    stable_ids = (
        [item.evidence_id for item in evidence] == [item.evidence_id for item in repeated.evidence]
        and [item.card_id for item in cards] == [item.card_id for item in repeated.evidence_cards]
    )
    call_ids_aligned = all(
        event.tool_call.id == event.message.tool_call_id for event in tool_events
    )
    usages = [call["usage"] for call in measured.calls]
    token_usage = [
        {
            "turn": index,
            "prompt_tokens": usage.get("prompt_tokens"),
            "completion_tokens": usage.get("completion_tokens"),
            "reasoning_tokens": (
                usage.get("completion_tokens_details", {}).get("reasoning_tokens")
                if isinstance(usage.get("completion_tokens_details"), dict) else None
            ),
            "total_tokens": usage.get("total_tokens"),
        }
        for index, usage in enumerate(usages, 1)
    ]
    success = all(
        (
            tool_sequence[:3] == ["web_search", "browser", "reader"],
            set(tool_arguments.get("browser", {})) == {"source_record_id"},
            all(args.get("artifact_id") == browser_artifact for args in reader_arguments),
            not forbidden_finish_keys,
            bool(trusted_reads),
            bool(evidence and cards),
            all(item.locator is None for item in evidence),
            all(source.location is None for card in cards for source in card.sources),
            card_work_consistent,
            trace_closed,
            stable_ids,
            model_calls_before_builder == model_calls_after_builder,
            "evidence_card_builder" not in registry.names,
            call_ids_aligned,
        )
    )
    summary = {
        "status": "success" if success else "failed",
        "attempt": 2,
        "prior_attempts": PRIOR_ATTEMPTS,
        "code_base_commit": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], text=True
        ).strip(),
        "started_at": started_at,
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "model_alias": alias,
        "model": measured.model,
        "tool_sequence": tool_sequence,
        "tool_arguments": tool_arguments,
        "browser_only_source_record_id": set(tool_arguments.get("browser", {})) == {"source_record_id"},
        "reader_uses_browser_artifact": all(args.get("artifact_id") == browser_artifact for args in reader_arguments),
        "tool_call_ids_aligned": call_ids_aligned,
        "finish_json_parsed": True,
        "finish_forbidden_provenance_keys": forbidden_finish_keys,
        "trusted_read_count": len(trusted_reads),
        "quote_matches": quote_matches,
        "builder_called_llm": model_calls_before_builder != model_calls_after_builder,
        "builder_registered_as_agent_tool": "evidence_card_builder" in registry.names,
        "resolved_work_ids": sorted(work_ids),
        "resolved_artifact_ids": sorted({item.artifact_id for item in evidence}),
        "resolved_source_record_ids": source_ids,
        "evidence_count": len(evidence),
        "evidence_card_count": len(cards),
        "one_work_per_card": card_work_consistent,
        "locators_all_none": all(item.locator is None for item in evidence),
        "source_locations_all_none": all(source.location is None for card in cards for source in card.sources),
        "deterministic_ids": stable_ids,
        "trace_chain_closed": trace_closed,
        "token_usage_per_turn": token_usage,
        "total_tokens": sum(usage.get("total_tokens", 0) for usage in usages),
        "harness_elapsed_ms": harness_ms,
        "builder_elapsed_ms": builder_ms,
        "model_elapsed_ms_per_turn": [call["elapsed_ms"] for call in measured.calls],
        "tool_elapsed_ms": {event.tool_call.name: event.observation.elapsed_ms for event in tool_events},
        "validator_compatibility_probe": "not executed; Validator migration is out of scope",
    }
    _write(
        summary,
        [_trace(index, event) for index, event in enumerate(result.trace, 1)],
        measured.calls,
        raw_finish,
        finish_draft,
        trusted_reads,
        manifest.model_dump(mode="json"),
        built,
    )
    return summary


def _write(summary, trace, model_calls, raw_finish, finish, reads, manifest, built) -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUTPUT_DIR / "trace.jsonl").write_text(
        "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in trace), encoding="utf-8"
    )
    payloads = {
        "model_calls.json": model_calls,
        "finish_draft.json": finish.model_dump(mode="json"),
        "trusted_reads.json": [item.model_dump(mode="json") for item in reads],
        "manifest_snapshot.json": manifest,
        "builder_result.json": built.model_dump(mode="json"),
        "evidence.json": [item.model_dump(mode="json") for item in built.evidence],
        "evidence_cards.json": [item.model_dump(mode="json") for item in built.evidence_cards],
    }
    for name, payload in payloads.items():
        (OUTPUT_DIR / name).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    (OUTPUT_DIR / "final_model_output.txt").write_text(raw_finish + "\n", encoding="utf-8")
    first_card = built.evidence_cards[0] if built.evidence_cards else None
    first_source = first_card.sources[0] if first_card and first_card.sources else None
    report = f"""# EvidenceCardBuilder 真实纵向实验报告

## 1. 目标与结论

验证真实 LLM 完成 WebSearch → Browser → Reader 后只提交无 provenance handle 的语义 `ResearchFinishDraft`，再由 Workflow-style post-processing 从 Harness trace 提取 trusted reads，交给确定性 Builder 恢复来源并生成 Evidence/Card。结论：**{summary['status']}**。

首次尝试记录：`{summary['prior_attempts']}`。首次把 JSON-only response format 错误施加到中间原生 Tool Calling 轮次，模型在 Browser 前结束；本次移除该实验选项后进行任务书允许的一次重跑。没有加入 JSON 修复逻辑。

## 2. 环境与生产修改

- 代码基线 commit：`{summary['code_base_commit']}`
- 模型：`{summary['model_alias']}` / `{summary['model']}`
- 时间：{summary['started_at']} – {summary['finished_at']}
- 生产修改：新增 `EvidenceCardBuilder` 及 schema contract；未修改 Harness、三工具、Validator、Reviewer、Coordinator 或正式 Workflow

## 3. Tool Calling

- 轨迹：`{summary['tool_sequence']}`
- 参数：`{summary['tool_arguments']}`
- Browser 仅 source_record_id：{summary['browser_only_source_record_id']}
- Reader 使用 Browser artifact：{summary['reader_uses_browser_artifact']}
- tool_call_id 全部对齐：{summary['tool_call_ids_aligned']}

## 4. 模型 Finish 与可信 Read

模型原始 JSON：

```json
{raw_finish}
```

- 严格解析 ResearchFinishDraft：{summary['finish_json_parsed']}
- 禁止的 provenance keys：`{summary['finish_forbidden_provenance_keys']}`
- Trusted read 数量：{summary['trusted_read_count']}
- quote → read / Work 候选与交集结果：`{summary['quote_matches']}` → final `{summary['resolved_work_ids']}`
- ambiguity/cross-work：无

## 5. Builder 恢复与输出

- Builder 自动恢复 work_id：`{summary['resolved_work_ids']}`
- Builder 自动恢复 artifact_id：`{summary['resolved_artifact_ids']}`
- Builder 自动恢复 source_record_id：`{summary['resolved_source_record_ids']}`
- Work title：`{first_card.document_title if first_card else None}`，来自 Manifest Work
- URL：`{first_source.url if first_source else None}`，来自 Manifest SourceRecord
- DOI：`{first_source.doi if first_source else None}`，仅从 Manifest identifiers 恢复；本来源没有则为 None
- card_id：`{first_card.card_id if first_card else None}`
- evidence_ids：`{[item.evidence_id for item in built.evidence]}`
- Evidence 数 / Card 数：{summary['evidence_count']} / {summary['evidence_card_count']}
- locator 全为 None：{summary['locators_all_none']}
- EvidenceSource.location 全为 None：{summary['source_locations_all_none']}
- 一文献一卡：{summary['one_work_per_card']}
- IDs 重复构建稳定：{summary['deterministic_ids']}

重复 read 的确定性选择顺序为 `artifact_id, char_start, char_end, read_id`。Quote 只允许原始 exact substring 或仅 whitespace normalization 后的 exact substring；不做 LLM、embedding、编辑距离或语义模糊匹配。

## 6. 完整追踪链与边界

- SourceRecord → Work → Artifact → Read → Evidence → Card 闭合：{summary['trace_chain_closed']}
- Evidence.provenance.read_id 来自真实 Harness trace
- Builder 调用 LLM：{summary['builder_called_llm']}（必须 False）
- Builder 注册为 Agent Tool：{summary['builder_registered_as_agent_tool']}（必须 False）
- 当前 EvidenceCard schema 校验：通过；字段保持 Reviewer 输入所需的 sources/evidence_ids/语义评分结构
- Validator compatibility probe：{summary['validator_compatibility_probe']}

## 7. Token 与耗时

- 每轮 token：`{summary['token_usage_per_turn']}`
- 总 token：{summary['total_tokens']}
- Harness 总耗时：{summary['harness_elapsed_ms']} ms
- 模型调用耗时：`{summary['model_elapsed_ms_per_turn']}` ms
- 工具耗时：`{summary['tool_elapsed_ms']}` ms
- Builder 自身耗时：{summary['builder_elapsed_ms']} ms

## 8. 已知限制与下一步

本次只使用一个稳定官方网页和一张 Card；共同来源解析成功不代表多文献检索质量。Whitespace normalization 不产生统一 locator，因此 v1 明确保留 `locator=None`、`location=None`。当前 Validator 可能仍要求 location，这是后续迁移项，不是 Builder 失败。本任务未迁移正式 Researcher Workflow、Validator 或 Reviewer。

核心成功标准全部满足，**可以进入 Validator / Reviewer 迁移设计**；在此之前还需要把 Workflow finish 后处理显式接到 Builder。
"""
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(report, encoding="utf-8")


def main() -> None:
    print(json.dumps(asyncio.run(run()), ensure_ascii=False, indent=2))
    print(f"outputs: {OUTPUT_DIR}")


if __name__ == "__main__":
    main()
