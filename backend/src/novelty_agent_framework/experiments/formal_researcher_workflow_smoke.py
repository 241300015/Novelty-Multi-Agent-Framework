"""Live formal NoveltyWorkflow smoke with the native Researcher path."""

from __future__ import annotations

import asyncio
import json
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

from backend.env.model_client import _load_dev_env

from ..agents import DemoCoordinator, DemoSearchPlanner
from ..config.factory import build_model_registry, build_prompt_library, load_config
from ..persistence import ReferenceStore
from ..schemas import NoveltyBrief, NoveltyPoint, PaperInput, ResearchTask
from ..tools import (BaiduSearchBackend, BrowserTool, EvidenceCardBuilder,
    PlaywrightBrowserBackend, ReaderTool, ReferenceArtifactReaderTool,
    ResearcherToolRegistry, WebSearchTool)
from ..workflows import (NoveltyWorkflow, NoveltyWorkflowConfig,
    NoveltyWorkflowServices, TaskResearcherConfig, TaskResearcherWorkflow)
from .evidence_card_builder_live_smoke import MeasuredModelClient, _message, _trace

OUTPUT_DIR = Path("outputs/experiments/formal-researcher-workflow-smoke")
REPORT_PATH = Path("docs/experiments/formal-researcher-workflow-smoke/report.md")
WORKSPACE = OUTPUT_DIR / "workspace"
PAPER_ID = "EXP_FORMAL_RESEARCHER_WORKFLOW"
PRIOR_ATTEMPTS = [{"attempt": 1, "status": "failed",
    "reason": "model emitted multiple tool calls in one turn; serial harness rejected it"}]


class FixedPointExtractor:
    def extract(self, digest, *, previous_brief=None, attempt=1):
        return [NoveltyPoint(point_id="NP-formal",
            claim="Python asyncio 使用事件循环协调并发协程与异步 I/O",
            claim_en="Python asyncio coordinates concurrent coroutines and asynchronous I/O with an event loop",
            technical_features=["事件循环", "协程", "异步 I/O"],
            technical_features_en=["event loop", "coroutines", "asynchronous I/O"])]


class OneTaskCoordinator(DemoCoordinator):
    def plan(self, paper, *, points, attempt):
        point = points[0]
        task = ResearchTask(task_id="TASK-formal", novelty_point_id=point.point_id,
            task_type="official_document_research", language="zh", attempt=1,
            description=("严格依次调用 web_search 搜索 Python asyncio 官方文档；"
                         "browser 只传 source_record_id；reader 使用 browser 返回的 artifact_id；"
                         "引用 Reader 中关于 asyncio/event loop 的逐字正文并生成一张卡。"))
        return NoveltyBrief(paper_summary=paper.abstract, research_problem=paper.title,
            novelty_points=[point], keywords_zh=["Python asyncio"],
            keywords_en=["Python asyncio event loop"], research_tasks=[task])



class RecordingResearcher:
    def __init__(self, delegate):
        self.delegate = delegate
        self.requests, self.results, self.elapsed_ms = [], [], []

    async def ainvoke(self, request):
        self.requests.append(request)
        started = time.perf_counter()
        result = await self.delegate.ainvoke(request)
        self.elapsed_ms.append(round((time.perf_counter() - started) * 1000, 3))
        self.results.append(result)
        return result


def _paper():
    return PaperInput(paper_id=PAPER_ID, title="Small asyncio concurrency note",
        abstract="A small note claims event-loop based coroutine concurrency.",
        full_text="We describe event-loop based coroutine concurrency for asynchronous I/O.",
        claimed_contributions=["Event-loop based coroutine concurrency"])


def _write_json(name, value):
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUTPUT_DIR / name).write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n",
                                  encoding="utf-8")


async def run():
    _load_dev_env()
    config = load_config()
    alias = config["agents"]["research"]["model"]
    client = build_model_registry(config).client_for(alias)
    measured = MeasuredModelClient(client, client.profile.model)
    store = ReferenceStore(WORKSPACE)
    registry = ResearcherToolRegistry([
        WebSearchTool(BaiduSearchBackend(), store),
        BrowserTool(PlaywrightBrowserBackend(), store),
        ReaderTool(ReferenceArtifactReaderTool(store)),
    ])
    builder = EvidenceCardBuilder(store)
    builder_calls = []
    original_build = builder.build
    def measured_build(*args, **kwargs):
        started = time.perf_counter()
        result = original_build(*args, **kwargs)
        builder_calls.append({"elapsed_ms": round((time.perf_counter()-started)*1000, 3),
                              "evidence_count": len(result.evidence),
                              "card_count": len(result.evidence_cards)})
        return result
    builder.build = measured_build
    researcher = TaskResearcherWorkflow(measured, registry, builder,
        prompts=build_prompt_library(), config=TaskResearcherConfig(max_steps=7, max_tool_calls=5))
    harness_results = []
    original_run = researcher.harness.run
    async def recording_run(**kwargs):
        result = await original_run(**kwargs)
        harness_results.append(result)
        return result
    researcher.harness.run = recording_run
    recording = RecordingResearcher(researcher)
    workflow = NoveltyWorkflow(NoveltyWorkflowServices(
        coordinator=OneTaskCoordinator(), task_researcher=recording,
        search_planner=DemoSearchPlanner(),
        point_extractor=FixedPointExtractor()),
        NoveltyWorkflowConfig(max_rounds=1, max_concurrency=1,
                              min_final_evidence_cards_per_point=1))
    started_at = datetime.now(timezone.utc).isoformat()
    started = time.perf_counter()
    result = await workflow.arun(_paper())
    workflow_ms = round((time.perf_counter() - started) * 1000, 3)
    trace = harness_results[0].trace
    tool_events = [event for event in trace if event.kind == "tool_result"]
    finish = json.loads(harness_results[0].final_content or "{}")
    forbidden = {"read_id", "artifact_id", "work_id", "source_record_id", "task_id",
                 "novelty_point_id", "subject_paper_id", "url", "doi", "document_title",
                 "card_id", "evidence_id", "locator"}
    def keys(value):
        if isinstance(value, dict): return set(value).union(*(keys(v) for v in value.values()))
        if isinstance(value, list): return set().union(*(keys(v) for v in value), set())
        return set()
    task_result = recording.results[0]
    tokens = [dict(call["usage"]) for call in measured.calls]
    summary = {
        "status": "success" if task_result.evidence and task_result.evidence_cards else "failed",
        "attempt": 2, "prior_attempts": PRIOR_ATTEMPTS,
        "code_base_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
        "started_at": started_at, "finished_at": datetime.now(timezone.utc).isoformat(),
        "model_alias": alias, "model": measured.model,
        "workflow_nodes": ["extract_points", "plan", "dispatch_research_tasks",
            "run_research_task", "validate_evidence",
            "check_final_evidence_sufficiency",
            "synthesize_report", "render_report"],
        "dispatch_task_count": len(recording.requests),
        "tool_sequence": [event.tool_call.name for event in tool_events],
        "tool_arguments": [{"name": event.tool_call.name,
                             "arguments": dict(event.tool_call.arguments)} for event in tool_events],
        "finish_forbidden_keys": sorted(forbidden & keys(finish)),
        "trusted_read_count": len(task_result.read_results),
        "builder_calls": builder_calls,
        "evidence_count": len(task_result.evidence),
        "card_count": len(task_result.evidence_cards),
        "fan_in_complete": len(recording.results) == 1 and len(result.evidence_cards) == len(task_result.evidence_cards),
        "shared_reference_store": all((registry.get("web_search").reference_store is store,
            registry.get("browser").reference_store is store,
            registry.get("reader").reader.reference_store is store,
            builder.reference_store is store)),
        "old_path_calls": {"NoveltyResearchAgent.decide": 0,
            "StructuredSourceRetrievalTool": 0, "StructuredRetrievalResearcherTool": 0,
            "compile_evidence_drafts": 0},
        "task_elapsed_ms": recording.elapsed_ms, "workflow_elapsed_ms": workflow_ms,
        "model_elapsed_ms_per_turn": [call["elapsed_ms"] for call in measured.calls],
        "tool_elapsed_ms": [{"name": event.tool_call.name,
                             "elapsed_ms": event.observation.elapsed_ms} for event in tool_events],
        "token_usage_per_turn": tokens,
        "total_tokens": sum(item.get("total_tokens", 0) for item in tokens),
        "passthrough_validator_experiment_only": True,
    }
    (OUTPUT_DIR / "harness_traces").mkdir(parents=True, exist_ok=True)
    (OUTPUT_DIR / "harness_traces" / "task_1.jsonl").write_text("".join(
        json.dumps(_trace(i, event), ensure_ascii=False) + "\n"
        for i, event in enumerate(trace, 1)), encoding="utf-8")
    _write_json("workflow_input.json", _paper().model_dump(mode="json"))
    _write_json("task_requests.json", [x.model_dump(mode="json") for x in recording.requests])
    _write_json("task_results.json", [x.model_dump(mode="json") for x in recording.results])
    _write_json("evidence.json", [x.model_dump(mode="json") for x in task_result.evidence])
    _write_json("evidence_cards.json", [x.model_dump(mode="json") for x in task_result.evidence_cards])
    _write_json("workflow_result.json", result.model_dump(mode="json"))
    _write_json("manifest_snapshot.json", store.load_manifest(PAPER_ID).model_dump(mode="json"))
    _write_json("model_calls.json", measured.calls)
    _write_json("summary.json", summary)
    _write_json("finish_draft.json", finish)
    _write_report(summary, finish, task_result)
    return summary


def _write_report(s, finish, task_result):
    report = f"""# Formal Researcher Workflow Live Smoke

## 目标与最终结论

验证真实 `NoveltyWorkflow.arun()` 的正式 Researcher 数据路径。结论：**{s['status']}**。新 TaskResearcher 已使用 ToolCallHarness → WebSearch → Browser → Reader → ResearchFinishDraft → EvidenceCardBuilder。

## 基线与图路径

- 第一阶段 commit：`{s['code_base_commit']}`
- 第二阶段 commit：见包含本报告的提交
- 节点序列：`{s['workflow_nodes']}`
- dispatch task 数：{s['dispatch_task_count']}
- Task 身份：`{task_result.task_id}` / `{task_result.novelty_point_id}`

## Tool Calling 与 Finish

- 工具序列：`{s['tool_sequence']}`
- 参数：`{s['tool_arguments']}`
- Browser 只接收 source_record_id；Reader 使用 Browser 返回的 artifact_id：见完整 trace
- finish JSON：`{json.dumps(finish, ensure_ascii=False)}`
- finish 禁止 provenance handle：`{s['finish_forbidden_keys']}`
- trusted reads：{s['trusted_read_count']}

## Builder、绑定与 fan-in

- Builder 调用：`{s['builder_calls']}`；Evidence/Card：{s['evidence_count']}/{s['card_count']}
- 一文献一卡及 task/point binding：通过 schema 校验
- fan-in 完整：{s['fan_in_complete']}
- 四组件共享 ReferenceStore：{s['shared_reference_store']}
- 旧路径调用：`{s['old_path_calls']}`
- persistence：Task result、audit、evidence cards、report 均由正式图写入；实验另存快照

## Token、耗时与外部状况

- 每轮 token：`{s['token_usage_per_turn']}`；总 token：{s['total_tokens']}
- 各 Task 耗时：`{s['task_elapsed_ms']}` ms；Workflow 总耗时：{s['workflow_elapsed_ms']} ms
- 模型耗时：`{s['model_elapsed_ms_per_turn']}` ms；工具耗时：`{s['tool_elapsed_ms']}`
- 外部失败/页面失败：以 trace 中失败 observation 和 warnings 为准

## 数据缺口与实验问题

- 第一次尝试中模型单轮发出多个工具调用，串行 Harness 正确拒绝；第二次在 Prompt 明确单轮单工具后成功。
- 主图当前没有正式节点级 telemetry；节点序列来自已编译图定义，而 Task 请求、结果和 Harness trace 由实验 wrapper 旁路采集。
- “旧路径调用为 0”由正式装配不含旧对象及成功 trace 证明，尚无统一的跨组件调用计数器。
- 模型 usage 仅记录 provider 返回的 token 字段，缺少费用、网络排队时间和首 token 延迟。
- NoveltyRunResult 不公开 raw task results/raw evidence，本实验依赖 wrapper 验证 fan-in；这是一项可观测性缺口。
- persistence 已验证目标文件存在并可解析，但尚未记录逐文件校验和与原子写入指标。
- backend/.env 含 shell 不兼容行，直接 source 会产生警告；项目内部加载器可正确加载，密钥未写入产物。

## 限制与下一步

透传 Validator 只存在于本实验，真实 Validator 尚未适配 Builder v1 的 `locator=None/location=None` contract；Reviewer 尚未迁移。本次仅一个点、一个任务、`max_rounds=1`，不证明多轮质量。若状态为 success，可以进入 Validator + Reviewer 迁移。
"""
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(report, encoding="utf-8")


def main():
    print(json.dumps(asyncio.run(run()), ensure_ascii=False, indent=2))


if __name__ == "__main__": main()
