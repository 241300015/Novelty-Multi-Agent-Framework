#!/usr/bin/env python3
"""Run the isolated PDF -> MinerU -> bootstrap -> workflow experiment."""

from __future__ import annotations

import argparse
import asyncio
import contextvars
import json
import os
import random
import subprocess
import sys
import threading
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "backend" / "src"))

from backend.env.model_client import OpenAICompatibleChatClient, _load_dev_env
from novelty_agent_framework.agents import DefaultEvidenceValidator, EvidenceValidationConfig
from novelty_agent_framework.config import (
    build_model_registry,
    build_standard_full_workflow,
    effective_safe_config,
    load_application_config,
)
from novelty_agent_framework.persistence import persist_paper_input
from novelty_agent_framework.core.run_identity import file_run_identity
from novelty_agent_framework.processing import DefaultPaperProcessor
from novelty_agent_framework.processing.mineru_parser import MineruSettings
from novelty_agent_framework.schemas import NoveltyPoint, PaperInput

DATE_ID = datetime.now().astimezone().strftime("%Y%m%d")
EXPERIMENT_ID = f"MG19333vrw_FullPipeline_{DATE_ID}"
SAMPLE_EXPERIMENT_ID = "MF2033k6lC_LocatorDisabled_SampledOneTask"
DEFAULT_PAPER_ID = "MG19333vrw-locator-off-full"
EXPERIMENT_DIR = PROJECT_ROOT / "docs" / "experiments" / EXPERIMENT_ID
# bootstrap 会以约 4 秒间隔连续请求 arXiv 数分钟；紧接着工作流还要继续检索，
# 不给一段冷却窗口时整体很容易触发 429（实测 6 条检索执行因此失败）。
BOOTSTRAP_COOLDOWN_SECONDS = 20.0
_call_context: contextvars.ContextVar[dict[str, str]] = contextvars.ContextVar(
    "experiment_call_context", default={}
)
MODEL_OVERRIDE_ENV_NAMES = (
    "NOVELTY_COORDINATOR_MODEL",
    "NOVELTY_POINT_EXTRACTOR_MODEL",
    "NOVELTY_RESEARCHER_MODEL",
    "NOVELTY_RESEARCH_MODEL",
    "NOVELTY_SEARCH_PLANNER_MODEL",
    "NOVELTY_REVIEWER_MODEL",
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def safe_error(exc: BaseException) -> str:
    text = str(exc)
    for secret_name in ("api_key", "authorization", "cookie"):
        if secret_name in text.casefold():
            return f"{type(exc).__name__}: <redacted>"
    return f"{type(exc).__name__}: {text}"[:1000]


class Recorder:
    def __init__(self) -> None:
        self.model_calls: list[dict[str, Any]] = []
        self.stage_elapsed: defaultdict[str, float] = defaultdict(float)
        self.task_elapsed: defaultdict[str, defaultdict[str, float]] = defaultdict(
            lambda: defaultdict(float)
        )
        self.tool_events: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
        self._lock = threading.Lock()

    def timed(self, stage: str, function: Callable[..., Any], *, task: str = "") -> Any:
        context = {"stage": stage, "task": task}
        token = _call_context.set(context)
        started = time.perf_counter()
        try:
            return function()
        finally:
            elapsed = time.perf_counter() - started
            with self._lock:
                self.stage_elapsed[stage] += elapsed
                if task:
                    self.task_elapsed[task][stage] += elapsed
            _call_context.reset(token)

    async def atimed(self, stage: str, function: Callable[..., Any], *, task: str = "") -> Any:
        context = {"stage": stage, "task": task}
        token = _call_context.set(context)
        started = time.perf_counter()
        try:
            return await function()
        finally:
            elapsed = time.perf_counter() - started
            with self._lock:
                self.stage_elapsed[stage] += elapsed
                if task:
                    self.task_elapsed[task][stage] += elapsed
            _call_context.reset(token)

    def install_model_hook(self) -> Callable[..., Any]:
        original = OpenAICompatibleChatClient.complete
        recorder = self

        def measured(client, messages, *, options=None):
            started_at = utc_now()
            started = time.perf_counter()
            context = dict(_call_context.get())
            try:
                response = original(client, messages, options=options)
            except BaseException as exc:
                recorder._append_model_call(
                    client, context, started_at, time.perf_counter() - started,
                    {}, False, safe_error(exc),
                )
                raise
            recorder._append_model_call(
                client, context, started_at, time.perf_counter() - started,
                dict(response.usage), True, None,
            )
            return response

        OpenAICompatibleChatClient.complete = measured
        return original

    def _append_model_call(
        self, client, context, started_at, elapsed, usage, success, error
    ) -> None:
        prompt = usage.get("prompt_tokens", usage.get("input_tokens"))
        completion = usage.get("completion_tokens", usage.get("output_tokens"))
        total = usage.get("total_tokens")
        if total is None and prompt is not None and completion is not None:
            total = prompt + completion
        row = {
            "model_alias": client.profile.alias,
            "model": client.profile.model,
            "stage": context.get("stage") or "unattributed",
            "research_task": context.get("task") or None,
            "started_at": started_at,
            "elapsed_seconds": round(elapsed, 6),
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "total_tokens": total,
            "raw_usage": usage,
            "usage_status": "reported" if usage else "unreported",
            "success": success,
        }
        if error:
            row["error_type"] = error.split(":", 1)[0]
            row["error"] = error
        with self._lock:
            self.model_calls.append(row)


def _wrap_workflow(workflow, recorder: Recorder) -> None:
    def sync_method(obj, name: str, stage: str, task_from_args=None) -> None:
        original = getattr(obj, name)

        def wrapped(*args, **kwargs):
            task = task_from_args(args, kwargs) if task_from_args else ""
            return recorder.timed(stage, lambda: original(*args, **kwargs), task=task)

        setattr(obj, name, wrapped)

    sync_method(workflow.point_extractor, "extract", "PointExtractor")
    sync_method(workflow.services.coordinator, "plan", "Coordinator")
    sync_method(workflow.services.coordinator, "plan_supplement", "Supplement")
    sync_method(workflow.services.coordinator, "synthesize", "Report synthesis/render")
    sync_method(
        workflow.services.search_planner,
        "plan",
        "SearchPlanner",
        lambda args, _: f"{args[0].point_id}/{args[1].task_id}",
    )
    if workflow.services.reviewer is not None:
        sync_method(workflow.services.reviewer, "review", "Reviewer")
    sync_method(workflow.validator, "validate", "Validator")

    researcher = workflow.services.task_researcher
    original_ainvoke = researcher.ainvoke

    async def measured_ainvoke(request):
        key = f"{request.novelty_point.point_id}/{request.research_task.task_id}"
        return await recorder.atimed(
            "Researcher", lambda: original_ainvoke(request), task=key
        )

    researcher.ainvoke = measured_ainvoke
    original_harness_run = researcher.harness.run

    async def measured_harness_run(*args, **kwargs):
        try:
            result = await original_harness_run(*args, **kwargs)
            trace = result.trace
        except Exception as exc:
            trace = getattr(exc, "trace", ())
            _record_trace(recorder, trace)
            raise
        _record_trace(recorder, trace)
        return result

    researcher.harness.run = measured_harness_run


def _record_trace(recorder: Recorder, trace) -> None:
    task = _call_context.get().get("task", "unattributed")
    for event in trace:
        observation = getattr(event, "observation", None)
        if getattr(event, "kind", "") == "tool_result" and observation is not None:
            recorder.tool_events[task].append(
                {
                    "tool": observation.tool_name,
                    "success": observation.succeeded,
                    "elapsed_ms": observation.elapsed_ms,
                    "error": observation.error,
                    "summary": observation.summary,
                }
            )
        elif getattr(event, "kind", "") == "error":
            recorder.tool_events[task].append(
                {"tool": None, "success": False, "detail": getattr(event, "detail", "")}
            )


def _configure_processor(config, recorder: Recorder) -> DefaultPaperProcessor:
    processing = config.project.processing
    registry = build_model_registry(config)
    ocr = registry.client_for(processing["ocr_model"]) if processing.get("ocr_model") else None
    title = registry.client_for(processing["llm_model"]) if processing.get("llm_model") else None
    return DefaultPaperProcessor(
        parser="mineru",
        ocr_client=ocr,
        llm_client=title,
        dpi=int(processing.get("dpi", 200)),
        min_chars_per_page=int(processing.get("quality_min_chars_per_page", 200)),
        mineru_settings=MineruSettings(
            python_path=processing.get("mineru_python"),
            env_name=processing.get("mineru_env", "mineru"),
            worker_path=processing.get("mineru_worker", "scripts/mineru_worker.py"),
            backend=processing.get("mineru_backend", "pipeline"),
            method=processing.get("mineru_method", "auto"),
            lang=processing.get("mineru_lang", "ch"),
            effort=processing.get("mineru_effort", "medium"),
            timeout_seconds=int(processing.get("mineru_timeout_seconds", 1800)),
            work_root=processing.get("mineru_work_root", "outputs/.mineru"),
            model_source=processing.get("mineru_model_source"),
        ),
    )


def _bootstrap(paper_id: str) -> tuple[dict[str, Any], float]:
    started = time.perf_counter()
    command = [
        sys.executable, str(PROJECT_ROOT / "scripts" / "reference_bootstrap.py"),
        "--paper-id", paper_id, "--output-root", "outputs", "--provider", "arxiv",
    ]
    completed = subprocess.run(command, cwd=PROJECT_ROOT, capture_output=True, text=True)
    elapsed = time.perf_counter() - started
    lines = [line for line in completed.stdout.splitlines() if line.strip()]
    payload = json.loads(lines[-1]) if lines else {}
    payload.update(
        success=completed.returncode == 0,
        elapsed_seconds=round(elapsed, 6),
        stderr=completed.stderr[-2000:],
    )
    if completed.returncode == 0 and BOOTSTRAP_COOLDOWN_SECONDS > 0:
        time.sleep(BOOTSTRAP_COOLDOWN_SECONDS)
        payload["cooldown_seconds"] = BOOTSTRAP_COOLDOWN_SECONDS
    return payload, elapsed


def _token_summary(calls: list[dict[str, Any]]) -> tuple[dict[str, Any], dict[str, Any]]:
    totals = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0,
              "calls": len(calls), "unreported_calls": 0}
    roles: defaultdict[str, Counter] = defaultdict(Counter)
    for call in calls:
        if call["usage_status"] == "unreported":
            totals["unreported_calls"] += 1
        for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
            value = call[key]
            if value is not None:
                totals[key] += value
                roles[call["stage"]][key] += value
        roles[call["stage"]]["calls"] += 1
    return totals, {key: dict(value) for key, value in sorted(roles.items())}


def _cost_summary(calls: list[dict[str, Any]], *, full_tasks: int | None = None) -> dict[str, Any]:
    """Apply the SiliconFlow rate card effective on the experiment date."""

    rates = {
        "deepseek-ai/DeepSeek-V4-Flash": {
            "cache_miss_input_cny_per_million": 1.0,
            "cache_hit_input_cny_per_million": 0.02,
            "output_cny_per_million": 2.0,
        },
        "deepseek-ai/DeepSeek-R1-0528-Qwen3-8B": {
            "cache_miss_input_cny_per_million": 0.0,
            "cache_hit_input_cny_per_million": 0.0,
            "output_cny_per_million": 0.0,
        },
    }
    by_model: defaultdict[str, dict[str, Any]] = defaultdict(
        lambda: {"calls": 0, "cache_miss_input_tokens": 0,
                 "cache_hit_input_tokens": 0, "output_tokens": 0, "cost_cny": 0.0}
    )
    by_stage: defaultdict[str, float] = defaultdict(float)
    for call in calls:
        model = call["model"]
        usage = call.get("raw_usage", {})
        hit = int(usage.get("prompt_cache_hit_tokens", 0) or 0)
        prompt = int(usage.get("prompt_tokens", 0) or 0)
        miss = int(usage.get("prompt_cache_miss_tokens", prompt - hit) or 0)
        output = int(usage.get("completion_tokens", 0) or 0)
        rate = rates.get(model)
        row = by_model[model]
        row["calls"] += 1
        row["cache_miss_input_tokens"] += miss
        row["cache_hit_input_tokens"] += hit
        row["output_tokens"] += output
        if rate is None:
            row["pricing_status"] = "unpriced"
            continue
        cost = (
            miss * rate["cache_miss_input_cny_per_million"]
            + hit * rate["cache_hit_input_cny_per_million"]
            + output * rate["output_cny_per_million"]
        ) / 1_000_000
        row["cost_cny"] += cost
        by_stage[call["stage"]] += cost
    for model, row in by_model.items():
        row["rates"] = rates.get(model)
        row["cost_cny"] = round(row["cost_cny"], 8)
    total = sum(row["cost_cny"] for row in by_model.values())
    result: dict[str, Any] = {
        "currency": "CNY",
        "rate_card_effective_at": "2026-08-31 Asia/Shanghai",
        "pricing_source": "https://siliconflow.cn/pricing",
        "future_pricing_notice": "https://api-docs.siliconflow.cn/docs/release-notes/overview",
        "actual_cost_cny": round(total, 8),
        "by_model": dict(by_model),
        "formula": "(cache_miss_input * miss_rate + cache_hit_input * hit_rate + output * output_rate) / 1,000,000",
        "notes": [
            "MinerU local inference cost is not token-billed and is excluded.",
            "Network/browser/arXiv tools have no metered price in this experiment.",
            "Platform discounts, coupons, taxes, and account-specific billing adjustments are excluded.",
        ],
    }
    if full_tasks:
        fixed = total - by_stage.get("Researcher", 0.0)
        sampled_task = by_stage.get("Researcher", 0.0)
        estimate = fixed + sampled_task * full_tasks
        result["full_scale_estimate"] = {
            "estimated_tasks": full_tasks,
            "estimated_cost_cny": round(estimate, 8),
            "rough_cost_range_cny": {
                "lower_0.8x_sampled_researcher": round(fixed + sampled_task * full_tasks * 0.8, 8),
                "upper_1.5x_sampled_researcher": round(fixed + sampled_task * full_tasks * 1.5, 8),
            },
        }
    return result


def _research_tasks(
    workspace: Path,
    recorder: Recorder,
    expected_keys: set[str],
) -> list[dict[str, Any]]:
    rows = []
    for path in sorted((workspace / "research-runs").glob("*/*/attempt-*.json")):
        result = json.loads(path.read_text(encoding="utf-8"))
        key = f'{result["novelty_point_id"]}/{result["task_id"]}'
        if key not in expected_keys:
            continue
        events = recorder.tool_events.get(key, [])
        tools: defaultdict[str, Counter] = defaultdict(Counter)
        counters = Counter()
        for event in events:
            name = event.get("tool")
            if name:
                tools[name]["attempts"] += 1
                tools[name]["success"] += int(event.get("success", False))
                if event.get("success") and "0" in event.get("summary", ""):
                    counters["zero_hit_count"] += 1
            detail = (event.get("detail") or event.get("error") or "").casefold()
            counters["tool_rejection_count"] += int("required" in detail or "budget" in detail)
            counters["multiple_tool_call_dropped_count"] += int("multiple tool calls" in detail)
            counters["required_reader_correction_count"] += int("reader required" in detail)
            counters["tool_execution_failure_count"] += int(name is not None and not event.get("success"))
        task_calls = [c for c in recorder.model_calls if c.get("research_task") == key]
        rows.append({
            "point": result["novelty_point_id"], "task": result["task_id"],
            "attempt": int(path.stem.split("-")[-1]), "status": result["status"],
            "planner_seconds": recorder.task_elapsed[key].get("SearchPlanner", 0),
            "researcher_seconds": recorder.task_elapsed[key].get("Researcher", 0),
            "tool_calls": {name: dict(count) for name, count in tools.items()},
            "tool_counters": dict(counters),
            "tokens": sum(c.get("total_tokens") or 0 for c in task_calls),
            "model_calls": len(task_calls), "warnings": result.get("warnings", []),
        })
    return rows


def _write_outputs(metrics: dict[str, Any], result: Any | None) -> None:
    EXPERIMENT_DIR.mkdir(parents=True, exist_ok=True)
    (EXPERIMENT_DIR / "metrics.json").write_text(
        json.dumps(metrics, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (EXPERIMENT_DIR / "effective-config.json").write_text(
        json.dumps(metrics.get("effective_config", {}), ensure_ascii=False, indent=2)
        + "\n",
        encoding="utf-8",
    )
    with (EXPERIMENT_DIR / "model_calls.jsonl").open("w", encoding="utf-8") as stream:
        for call in metrics.get("model_calls", []):
            stream.write(json.dumps(call, ensure_ascii=False) + "\n")
    (EXPERIMENT_DIR / "README.md").write_text(
        f"# {metrics.get('experiment_id')}\n\n"
        "See `report.md`, `effective-config.json`, `metrics.json`, and "
        "`model_calls.jsonl`.\n", encoding="utf-8"
    )
    evidence = metrics.get("evidence", {})
    timing = metrics.get("timing", {})
    report = f"""# {metrics.get('experiment_id')} 实验报告

## 1. 实验目的

验证 SearchPlanner 默认模型收敛为 `deepseek-flash` 后，不设置任何
`NOVELTY_*_MODEL` 角色覆盖即可完成 MG19333vrw 工作流并生成报告。

## 2. 基线

- branch: `{metrics.get('git_branch')}`
- commit: `{metrics.get('git_commit')}`
- started_at: `{metrics.get('started_at')}`
- execution_status: **{metrics.get('execution_status', 'INCOMPLETE')}**
- status: **{metrics.get('status')}**

## 3. 固定实验条件

- `paper = MG19333vrw`
- Prompt 与工具预算保持当前仓库配置
- `max_rounds = 1`
- `require_direct_quote = true`
- `require_source_location = false`
- `locator_gate_disabled = true`

## 4. 输入

- PDF: `{metrics.get('input_pdf')}`
- size: {metrics.get('input_size_bytes')} bytes
- pages: {metrics.get('paper_processing', {}).get('pages')}

## 5. 实际生效配置

- `NOVELTY_*_MODEL` 覆盖：{metrics.get('model_overrides', {}).get('active') or '无'}
- 无覆盖完整运行验收：**{'PASS' if metrics.get('verification', {}).get('criteria_passed') else 'FAIL'}**
- SearchPlanner 实际调用模型：{metrics.get('verification', {}).get('observed_search_planner_models')}
- 工作流正常返回：{metrics.get('verification', {}).get('workflow_returned')}
- 质量门结果：{metrics.get('status')}（与“进程完成”分开记录）
- 完整无密钥快照：`effective-config.json`

```json
{json.dumps(metrics.get('effective_config', {}), ensure_ascii=False, indent=2)}
```

## 6. MinerU

```json
{json.dumps(metrics.get('paper_processing', {}), ensure_ascii=False, indent=2)}
```

## 7. Reference Bootstrap

```json
{json.dumps(metrics.get('reference_bootstrap', {}), ensure_ascii=False, indent=2)}
```

## 8. 工作流结果

- rounds: {metrics.get('workflow', {}).get('rounds')}
- ResearchTasks: {len(metrics.get('research_tasks', []))}
- insufficient final evidence points: {metrics.get('workflow', {}).get('insufficient_final_evidence_points')}

## 9. Tool 调用统计

```json
{json.dumps(metrics.get('research_tasks', []), ensure_ascii=False, indent=2)}
```

## 10. Evidence

```json
{json.dumps(evidence, ensure_ascii=False, indent=2)}
```

## 11. 时间统计

```json
{json.dumps(timing, ensure_ascii=False, indent=2)}
```

## 12. Token 统计

```json
{json.dumps({'total': metrics.get('tokens'), 'by_role': metrics.get('token_by_role')}, ensure_ascii=False, indent=2)}
```

MinerU external API tokens: 0  
MinerU internal inference tokens: N/A

## 13. 最终报告

- path: `{metrics.get('report', {}).get('path')}`
- generated: {metrics.get('report', {}).get('generated')}

## 14. 与上次实验对比

| 指标 | 上轮 Full Workflow | 本轮 Full Pipeline |
|---|---:|---:|
| PDF parser | text_layer | {metrics.get('paper_processing', {}).get('source')} |
| NoveltyPoints | 2 | {metrics.get('workflow', {}).get('novelty_points')} |
| ResearchTasks | 4 | {len(metrics.get('research_tasks', []))} |
| EvidenceCards built | 3 | {evidence.get('built')} |
| Validator accepted | 0 | {evidence.get('validator_accepted')} |
| Reviewer accepted | 0 | {evidence.get('reviewer_accepted')} |
| 有效最终报告 | ❌ | {metrics.get('report', {}).get('valid')} |
| 总耗时 | ≈35 min | {timing.get('total_seconds')} s |
| Total tokens | 未完整记录 | {metrics.get('tokens', {}).get('total_tokens')} |
| PDF→Report 总耗时 | 未记录 | {timing.get('total_seconds')} s |

## 15. 暴露的新问题

{metrics.get('error') or '见 metrics.json 中的 issues / rejected reasons。'}

## 16. 结论

{metrics.get('conclusion', '实验未完成。')}

## 17. 完整规模估算

```json
{json.dumps(metrics.get('full_scale_estimate', {}), ensure_ascii=False, indent=2)}
```

## 18. 金额成本

```json
{json.dumps(metrics.get('cost', {}), ensure_ascii=False, indent=2)}
```
"""
    (EXPERIMENT_DIR / "report.md").write_text(report, encoding="utf-8")


def main() -> int:
    _load_dev_env()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pdf", type=Path, default=Path("examples/MG19333vrw.pdf"))
    parser.add_argument("--paper-id", default=DEFAULT_PAPER_ID)
    parser.add_argument(
        "--resume", action="store_true",
        help="Reuse persisted MinerU PaperInput, bootstrap manifest, and novelty points.",
    )
    parser.add_argument("--sample-one-task", action="store_true")
    parser.add_argument("--seed", type=int, default=20260831)
    parser.add_argument(
        "--experiment-id",
        default=None,
        help=(
            "实验输出目录名（docs/experiments/<id>）。缺省沿用内置常量，"
            "换论文跑时请显式指定，以免覆盖历史实验报告。"
        ),
    )
    args = parser.parse_args()
    global EXPERIMENT_DIR
    if args.experiment_id:
        EXPERIMENT_DIR = PROJECT_ROOT / "docs" / "experiments" / args.experiment_id
    source_metrics_path = EXPERIMENT_DIR / "metrics.json"
    if args.sample_one_task:
        EXPERIMENT_DIR = PROJECT_ROOT / "docs" / "experiments" / SAMPLE_EXPERIMENT_ID
    total_started = time.perf_counter()
    recorder = Recorder()
    previous_metrics: dict[str, Any] = {}
    if args.resume and source_metrics_path.is_file():
        previous_metrics = json.loads(source_metrics_path.read_text(encoding="utf-8"))
    original_complete = recorder.install_model_hook()
    metrics: dict[str, Any] = {
        "experiment_id": args.experiment_id
        or (SAMPLE_EXPERIMENT_ID if args.sample_one_task else EXPERIMENT_ID),
        "paper_id": args.paper_id,
        "started_at": utc_now(), "status": "RUNNING", "input_pdf": str(args.pdf),
        "input_size_bytes": args.pdf.stat().st_size if args.pdf.is_file() else None,
        "configuration": {"locator_gate_disabled": True, "require_direct_quote": True,
                          "require_source_location": False, "parser": "mineru",
                          "resumed": args.resume, "sample_one_task": args.sample_one_task,
                          "random_seed": args.seed if args.sample_one_task else None},
    }
    result = None
    try:
        if not args.pdf.is_file():
            raise FileNotFoundError(f"任务书指定的原始 PDF 不存在: {args.pdf}")
        metrics["git_commit"] = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=PROJECT_ROOT, text=True
        ).strip()
        metrics["git_branch"] = subprocess.check_output(
            ["git", "branch", "--show-current"], cwd=PROJECT_ROOT, text=True
        ).strip()
        config = load_application_config()
        # This experiment is intentionally a single-round control for both the
        # sampled and full MG19333vrw runs.
        config.project.workflow.max_rounds = 1
        # Production full-pipeline preflight must happen before MinerU,
        # bootstrap, search, or any other costly work begins.
        workflow = build_standard_full_workflow(config)
        metrics["model_overrides"] = {
            "active": [name for name in MODEL_OVERRIDE_ENV_NAMES if os.environ.get(name)],
            "verified_without_overrides": not any(
                os.environ.get(name) for name in MODEL_OVERRIDE_ENV_NAMES
            ),
        }
        workspace = PROJECT_ROOT / "outputs" / args.paper_id
        fixed_points = None
        if args.resume:
            paper_path = workspace / "paper-input" / "others" / "paper.json"
            points_path = workspace / "novelty-points.json"
            bootstrap_path = workspace / "subject_references" / "bootstrap.json"
            if not all(path.is_file() for path in (paper_path, points_path, bootstrap_path)):
                raise FileNotFoundError("--resume requires persisted paper.json, novelty-points.json, and bootstrap.json")
            paper = PaperInput.model_validate_json(paper_path.read_text(encoding="utf-8"))
            if paper.metadata.get("source") != "mineru":
                raise RuntimeError(f"Persisted PaperInput is not MinerU: {paper.metadata.get('source')}")
            fixed_points = [
                NoveltyPoint.model_validate(item)
                for item in json.loads(points_path.read_text(encoding="utf-8"))["novelty_points"]
            ]
            all_point_count = len(fixed_points)
            if args.sample_one_task:
                selected_point = random.Random(args.seed).choice(fixed_points)
                fixed_points = [selected_point]
                metrics["sampling"] = {
                    "seed": args.seed,
                    "available_novelty_points": all_point_count,
                    "selected_point_id": selected_point.point_id,
                    "selection_method": "random.Random(seed).choice",
                }
            processing_elapsed = 0.0
            bootstrap_elapsed = 0.0
            metrics["paper_processing"] = dict(previous_metrics.get("paper_processing", {}))
            metrics["paper_processing"].update(reused=True, success=True, source="mineru")
            metrics["reference_bootstrap"] = dict(previous_metrics.get("reference_bootstrap", {}))
            metrics["reference_bootstrap"].update(reused=True, success=True)
            metrics["resumed_from"] = {
                "previous_started_at": previous_metrics.get("started_at"),
                "previous_finished_at": previous_metrics.get("finished_at"),
                "reused_novelty_points": all_point_count,
                "previous_status": previous_metrics.get("status"),
            }
        else:
            processing_started = time.perf_counter()
            processor = _configure_processor(config, recorder)
            document = recorder.timed(
                "MinerU + Paper processing",
                lambda: processor.process(args.pdf, paper_id=args.paper_id),
            )
            paper = processor.to_paper_input(document)
            persist_paper_input(document, paper)
            processing_elapsed = time.perf_counter() - processing_started
            metrics["paper_processing"] = {
                "success": document.source == "mineru", "source": document.source,
                "pages": len(document.pages), "backend": config.project.processing.get("mineru_backend"),
                "method": config.project.processing.get("mineru_method"),
                "fallback_triggered": document.source != "mineru",
                "warnings": document.parse_warnings, "elapsed_seconds": round(processing_elapsed, 6),
            }
            if document.source != "mineru":
                raise RuntimeError(f"MinerU source requirement failed: {document.source}")
            bootstrap, bootstrap_elapsed = _bootstrap(args.paper_id)
            metrics["reference_bootstrap"] = bootstrap
            if not bootstrap["success"]:
                raise RuntimeError("Reference Bootstrap did not become ready")
        paper_path = workspace / "paper-input" / "others" / "paper.json"
        metrics["effective_config"] = effective_safe_config(config)
        if fixed_points is not None:
            class PersistedPointExtractor:
                def extract(self, *_args, **_kwargs):
                    return list(fixed_points)

            workflow.point_extractor = PersistedPointExtractor()
        if args.sample_one_task:
            coordinator = workflow.services.coordinator
            original_plan = coordinator.plan
            task_rng = random.Random(args.seed + 1)

            def sampled_plan(*plan_args, **plan_kwargs):
                brief = original_plan(*plan_args, **plan_kwargs)
                tasks = list(brief.research_tasks)
                if not tasks:
                    return brief
                selected = task_rng.choice(tasks)
                metrics["sampling"].update(
                    coordinator_tasks_for_selected_point=len(tasks),
                    selected_task_id=selected.task_id,
                    selected_task_language=selected.language,
                    full_scale_task_estimate=len(tasks) * metrics["sampling"]["available_novelty_points"],
                )
                return brief.model_copy(update={"research_tasks": [selected]})

            coordinator.plan = sampled_plan
        workflow.validator = DefaultEvidenceValidator(
            EvidenceValidationConfig(require_direct_quote=True, require_source_location=False)
        )
        _wrap_workflow(workflow, recorder)
        workflow_started = time.perf_counter()
        result = workflow.run(
            paper,
            run_identity=file_run_identity(
                "full_pipeline", paper_path, project_root=PROJECT_ROOT
            ),
        )
        workflow_elapsed = time.perf_counter() - workflow_started
        report_path = workspace / "report" / f"{args.paper_id}-report.md"
        raw_cards_path = workspace / "evidence-cards.json"
        raw_payload = json.loads(raw_cards_path.read_text(encoding="utf-8")) if raw_cards_path.exists() else {}
        built = len(raw_payload.get("raw_evidence_cards", []))
        validator_accepted = len(raw_payload.get("validator_accepted_cards", []))
        reviewer_accepted = len(result.evidence_cards)
        metrics["workflow"] = {
            "rounds": result.rounds, "novelty_points": len(result.brief.novelty_points),
            "insufficient_final_evidence_points": [
                item.model_dump(mode="json")
                for item in result.insufficient_final_evidence_points
            ],
            "issues": [item.model_dump(mode="json") for item in result.issues],
        }
        current_task_keys = {
            f"{task.novelty_point_id}/{task.task_id}"
            for task in result.brief.research_tasks
        }
        metrics["research_tasks"] = _research_tasks(
            workspace, recorder, current_task_keys
        )
        if args.sample_one_task:
            metrics["research_tasks"] = [
                row for row in metrics["research_tasks"]
                if row["point"] == metrics["sampling"]["selected_point_id"]
                and row["task"] == metrics["sampling"]["selected_task_id"]
            ]
        metrics["evidence"] = {
            "built": built, "validator_accepted": validator_accepted,
            "validator_rejected": len(result.rejected_evidence),
            "reviewer_accepted": reviewer_accepted,
            "reviewer_rejected": max(0, validator_accepted - reviewer_accepted),
            "rejected": [item.model_dump(mode="json") for item in result.rejected_evidence],
        }
        text = report_path.read_text(encoding="utf-8") if report_path.exists() else ""
        valid = bool(text.strip() and reviewer_accepted > 0 and "Traceback" not in text)
        metrics["report"] = {"generated": report_path.is_file(), "path": str(report_path),
                             "bytes": len(text.encode()), "valid": valid}
        observed_planner_models = sorted({
            call["model_alias"]
            for call in recorder.model_calls
            if call.get("stage") == "SearchPlanner" and call.get("model_alias")
        })
        metrics["verification"] = {
            "workflow_returned": True,
            "model_overrides_absent": metrics["model_overrides"][
                "verified_without_overrides"
            ],
            "configured_search_planner_model": config.search_planner.model.alias,
            "observed_search_planner_models": observed_planner_models,
            "max_rounds": config.project.workflow.max_rounds,
            "final_report_generated": report_path.is_file(),
        }
        metrics["verification"]["criteria_passed"] = all((
            metrics["verification"]["model_overrides_absent"],
            metrics["verification"]["configured_search_planner_model"]
            == "deepseek-flash",
            observed_planner_models == ["deepseek-flash"],
            metrics["verification"]["max_rounds"] == 1,
            metrics["verification"]["final_report_generated"],
        ))
        metrics["execution_status"] = "COMPLETED"
        metrics["timing"] = {
            "paper_processing_seconds": round(processing_elapsed, 6),
            "reference_bootstrap_seconds": round(bootstrap_elapsed, 6),
            "workflow_seconds": round(workflow_elapsed, 6),
            **{f"{key}_seconds": round(value, 6) for key, value in recorder.stage_elapsed.items()},
            "total_seconds": round(time.perf_counter() - total_started, 6),
            "previous_attempt_seconds": previous_metrics.get("timing", {}).get("total_seconds", 0),
        }
        metrics["status"] = "SUCCESS" if valid and validator_accepted > 0 else "INVALID / DEGRADED"
        metrics["conclusion"] = (
            "Full Pipeline Success；MinerU 真实成功且 locator gate 关闭后存在有效证据闭环。"
            if metrics["status"] == "SUCCESS" else
            "程序已闭环，但未同时满足 MinerU、Validator、Reviewer 和有效报告标准。"
        )
        if args.sample_one_task:
            full_tasks = metrics["sampling"].get("full_scale_task_estimate", 1)
            task_stages = {"SearchPlanner", "Researcher", "Reviewer"}
            task_tokens = sum(
                call.get("total_tokens") or 0
                for call in recorder.model_calls
                if call.get("stage") in task_stages
            )
            fixed_tokens = sum(call.get("total_tokens") or 0 for call in recorder.model_calls) - task_tokens
            fixed_workflow_seconds = sum(
                recorder.stage_elapsed.get(stage, 0.0)
                for stage in ("PointExtractor", "Coordinator", "Validator", "Reviewer", "Report synthesis/render")
            )
            sampled_task_seconds = sum(
                recorder.stage_elapsed.get(stage, 0.0)
                for stage in ("SearchPlanner", "Researcher")
            )
            preprocessing_seconds = float(metrics["paper_processing"].get("elapsed_seconds", 0))
            bootstrap_seconds = float(metrics["reference_bootstrap"].get("elapsed_seconds", 0))
            metrics["full_scale_estimate"] = {
                "method": "fixed cost + sampled per-task cost * estimated full task count",
                "estimated_tasks": full_tasks,
                "sampled_tasks": 1,
                "estimated_total_tokens": fixed_tokens + task_tokens * full_tasks,
                "estimated_workflow_seconds": fixed_workflow_seconds + sampled_task_seconds * full_tasks,
                "estimated_pdf_to_report_seconds": (
                    preprocessing_seconds + bootstrap_seconds
                    + fixed_workflow_seconds + sampled_task_seconds * full_tasks
                ),
                "caveats": [
                    "Linear extrapolation; task complexity and retries vary.",
                    "Parallel wall-clock can be lower than summed task elapsed.",
                    "Supplement rounds are excluded because the sample fixes max_rounds=1.",
                ],
            }
    except BaseException as exc:
        metrics["status"] = "INVALID / DEGRADED"
        metrics["error"] = safe_error(exc)
        metrics.setdefault("timing", {})["total_seconds"] = round(
            time.perf_counter() - total_started, 6
        )
    finally:
        OpenAICompatibleChatClient.complete = original_complete
        totals, by_role = _token_summary(recorder.model_calls)
        metrics["tokens"], metrics["token_by_role"] = totals, by_role
        metrics["model_calls"] = recorder.model_calls
        metrics["cost"] = _cost_summary(
            recorder.model_calls,
            full_tasks=(
                metrics.get("sampling", {}).get("full_scale_task_estimate")
                if args.sample_one_task else None
            ),
        )
        metrics["finished_at"] = utc_now()
        _write_outputs(metrics, result)
    print(json.dumps({"status": metrics["status"], "error": metrics.get("error"),
                      "metrics": str(EXPERIMENT_DIR / "metrics.json")}, ensure_ascii=False))
    return 0 if metrics.get("verification", {}).get("criteria_passed") else 1


if __name__ == "__main__":
    raise SystemExit(main())
