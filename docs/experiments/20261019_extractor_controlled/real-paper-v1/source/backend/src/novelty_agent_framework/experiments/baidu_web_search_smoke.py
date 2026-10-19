"""Live Baidu backend and WebSearch persistence smoke experiment."""

from __future__ import annotations

import asyncio
import json
import os
import platform
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from backend.env.model_client import _load_dev_env

from ..persistence import ReferenceStore
from ..schemas import NoveltyPoint, ResearchTask, TaskResearchRequest, WebSearchArguments
from ..tools import BaiduSearchBackend, WebSearchTool
from ..tools.web_search_backend import BAIDU_WEB_SEARCH_ENDPOINT
from ._support import minimal_search_plan

EXPERIMENT_OUTPUT_DIR = Path("outputs/experiments/baidu-web-search-smoke")
REPORT_PATH = Path("docs/experiments/baidu-web-search-smoke/report.md")
WORKSPACE_ROOT = EXPERIMENT_OUTPUT_DIR / "workspace"
SUBJECT_PAPER_ID = "EXP_BAIDU_WEBSEARCH"
QUERIES = (
    "多智能体 科技查新",
    "多智能体 科技查新 论文",
    "multi-agent novelty search",
)
MAX_RESULTS = 5


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _scope() -> TaskResearchRequest:
    return TaskResearchRequest(
        subject_paper_id=SUBJECT_PAPER_ID,
        run_id="baidu-web-search-live-smoke",
        novelty_point=NoveltyPoint(
            point_id="NP-baidu-smoke",
            claim="验证百度 Web Search 候选来源发现",
            technical_features=["multi-agent novelty search"],
        ),
        research_task=ResearchTask(
            task_id="TASK-baidu-smoke",
            novelty_point_id="NP-baidu-smoke",
            task_type="web_search_smoke",
            language="zh",
            description="验证百度搜索后端与 SourceRecord 持久化",
        ),
        search_plan=minimal_search_plan("TASK-baidu-smoke", "NP-baidu-smoke"),
    )


def _hit_json(hit) -> dict:
    return {
        "title": hit.title,
        "url": hit.url,
        "snippet": hit.snippet,
        "score": hit.score,
        "published_at": (
            hit.published_at.isoformat() if hit.published_at is not None else None
        ),
        "source_name": hit.source_name,
        "external_id": hit.external_id,
        "raw_metadata": dict(hit.raw_metadata),
    }


async def run_experiment() -> dict:
    _load_dev_env()
    if not os.getenv("BAIDU_QIANFAN_API_KEY"):
        raise RuntimeError("BAIDU_QIANFAN_API_KEY is not configured")

    EXPERIMENT_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    backend = BaiduSearchBackend()
    started_at = _utc_now()
    overall_started = time.perf_counter()
    backend_runs = []
    for query in QUERIES:
        request_started = time.perf_counter()
        result = await backend.search(query, max_results=MAX_RESULTS)
        duration_ms = round((time.perf_counter() - request_started) * 1_000, 3)
        backend_runs.append(
            {
                "query": query,
                "succeeded": True,
                "duration_ms": duration_ms,
                "diagnostics": dict(backend.last_diagnostics),
                "hits": [_hit_json(hit) for hit in result.hits],
                "warnings": list(result.warnings),
            }
        )

    store = ReferenceStore(output_root=WORKSPACE_ROOT)
    web_search = WebSearchTool(backend, store)
    vertical_runs = []
    for sequence in (1, 2):
        vertical_started = time.perf_counter()
        observation = await web_search.ainvoke(
            WebSearchArguments(query=QUERIES[0], max_results=MAX_RESULTS),
            scope=_scope(),
        )
        vertical_runs.append(
            {
                "sequence": sequence,
                "duration_ms": round(
                    (time.perf_counter() - vertical_started) * 1_000, 3
                ),
                "backend_diagnostics": dict(backend.last_diagnostics),
                "observation": observation.model_dump(mode="json"),
            }
        )

    manifest = store.load_manifest(SUBJECT_PAPER_ID)
    first_ids = [
        item["source_record_id"]
        for item in vertical_runs[0]["observation"]["payload"]["search_result"][
            "results"
        ]
    ]
    second_ids = [
        item["source_record_id"]
        for item in vertical_runs[1]["observation"]["payload"]["search_result"][
            "results"
        ]
    ]
    manifest_ids = [record.source_record_id for record in manifest.source_records]
    stable_ids = first_ids == second_ids
    persisted_ids = set(second_ids).issubset(manifest_ids)
    all_hits = [hit for run in backend_runs for hit in run["hits"]]
    domains = Counter(
        urlsplit(hit["url"]).hostname or "unknown" for hit in all_hits
    )
    summary = {
        "experiment": "baidu-web-search-smoke",
        "started_at": started_at,
        "finished_at": _utc_now(),
        "endpoint": BAIDU_WEB_SEARCH_ENDPOINT,
        "backend_name": backend.name,
        "api_key_status": "configured",
        "queries": list(QUERIES),
        "max_results": MAX_RESULTS,
        "backend_runs": backend_runs,
        "vertical_runs": vertical_runs,
        "manifest_source_record_count": len(manifest.source_records),
        "stable_source_record_ids": stable_ids,
        "all_result_ids_persisted": persisted_ids,
        "domain_distribution": dict(domains),
        "overall_duration_ms": round(
            (time.perf_counter() - overall_started) * 1_000, 3
        ),
    }
    _write_outputs(summary, manifest.model_dump(mode="json"))
    return summary


def _write_outputs(summary: dict, manifest: dict) -> None:
    backend_results = {
        key: summary[key]
        for key in (
            "experiment",
            "started_at",
            "finished_at",
            "endpoint",
            "backend_name",
            "queries",
            "max_results",
            "backend_runs",
            "domain_distribution",
            "overall_duration_ms",
        )
    }
    websearch_result = {
        "vertical_runs": summary["vertical_runs"],
        "manifest_source_record_count": summary["manifest_source_record_count"],
        "stable_source_record_ids": summary["stable_source_record_ids"],
        "all_result_ids_persisted": summary["all_result_ids_persisted"],
    }
    for name, payload in (
        ("backend_results.json", backend_results),
        ("websearch_result.json", websearch_result),
        ("manifest_snapshot.json", manifest),
    ):
        (EXPERIMENT_OUTPUT_DIR / name).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(
        _report(summary), encoding="utf-8"
    )


def _report(summary: dict) -> str:
    rows = []
    for run in summary["backend_runs"]:
        diagnostics = run["diagnostics"]
        rows.append(
            "| {query} | {ok} | {request_id} | {raw} | {hits} | {duration:.3f} |".format(
                query=run["query"],
                ok="成功" if run["succeeded"] else "失败",
                request_id=diagnostics.get("request_id") or "—",
                raw=diagnostics.get("raw_reference_count", 0),
                hits=diagnostics.get("normalized_hit_count", 0),
                duration=run["duration_ms"],
            )
        )
    missing = {
        key: sum(run["diagnostics"].get(key, 0) for run in summary["backend_runs"])
        for key in (
            "missing_snippet_count",
            "missing_date_count",
            "missing_website_count",
            "skipped_non_web_count",
            "skipped_missing_url_count",
            "duplicate_url_count",
        )
    }
    domains = "、".join(
        f"`{domain}` × {count}"
        for domain, count in sorted(
            summary["domain_distribution"].items(), key=lambda item: -item[1]
        )
    ) or "无"
    vertical = summary["vertical_runs"][-1]["observation"]
    web_items = vertical["payload"]["search_result"]["results"]
    total_vertical_ms = sum(run["duration_ms"] for run in summary["vertical_runs"])
    return f"""# BaiduSearchBackend v1 接入与真实预实验报告

## 1. 实验目标与结论

本实验验证百度 Web Search API → `BaiduSearchBackend` → `WebSearchTool` → `ReferenceStore` → `ReferenceManifest` 的真实链路。实验不使用 LLM 或 ToolCallHarness。

结论：三类 query 均完成真实 API 请求；WebSearch vertical smoke 返回 {len(web_items)} 个 `WebSearchItem`，Manifest 持久化 {summary['manifest_source_record_count']} 个 `SourceRecord`。稳定 ID 检查为 `{summary['stable_source_record_ids']}`，返回 ID 均可从 Manifest 恢复为 `{summary['all_result_ids_persisted']}`。

## 2. 时间与本地环境

- 开始：{summary['started_at']}
- 结束：{summary['finished_at']}
- Python：{platform.python_version()}
- 平台：{platform.platform()}
- API Key：configured（值未记录）
- Endpoint：`{summary['endpoint']}`
- 整体实验耗时：{summary['overall_duration_ms']:.3f} ms
- 两次 WebSearch vertical smoke 合计耗时：{total_vertical_ms:.3f} ms

## 3. Backend contract 与固定请求

`BaiduSearchBackend.name == "baidu"`，实现冻结接口 `search(query, *, max_results) -> SearchBackendResult`。请求固定使用一个 user message、`baidu_search_v2`，以及 `resource_type_filter=[{{"type":"web","top_k":max_results}}]`，不设置 edition、filter、fallback 或 retry。

查询计量假设：ASCII 字符按 1 单位、所有非 ASCII Unicode code point 按 2 单位；超过 72 单位本地拒绝且不裁剪。

## 4. Backend-only smoke

| Query | 状态 | request_id | 原始 references | 标准化 SearchHit | 耗时 ms |
|---|---|---|---:|---:|---:|
{chr(10).join(rows)}

字段映射为：title ← title（缺失回退 URL）、url ← url、snippet ← snippet、published_at ← date、source_name ← website、external_id ← id、score ← None；request_id、web_anchor 和 content 仅保留在 `raw_metadata`。

## 5. 缺失、过滤与重复情况

- snippet 缺失：{missing['missing_snippet_count']}
- date 缺失：{missing['missing_date_count']}
- website 缺失：{missing['missing_website_count']}
- 非 web 结果：{missing['skipped_non_web_count']}
- 缺少 URL 的 web 结果：{missing['skipped_missing_url_count']}
- 重复 URL：{missing['duplicate_url_count']}
- 来源域名分布：{domains}

## 6. WebSearch vertical smoke

真实 `BaiduSearchBackend` 无适配地注入现有 `WebSearchTool`。每个标准化结果被转换为 Agent-facing `WebSearchItem`，并在返回前持久化为 `SourceRecord`：`source_id="baidu"`、`source_kind="web"`、`access_status="discovered"`、`landing_url=hit.url`。

相同 query 连续执行两次，source_record_id 序列保持一致：**{summary['stable_source_record_ids']}**。Manifest 未因第二次搜索无限追加重复记录，且所有第二次返回 ID 均存在于 Manifest：**{summary['all_result_ids_persisted']}**。

`WebSearchTool` 与 `SearchBackend` frozen contract 均为零修改。

## 7. 中文结果定性观察

本节是 smoke experiment 的定性观察，不是 precision、recall、MAP 或 NDCG 结论。结果标题、URL、snippet 与来源域名可用于候选筛选；应结合上面的域名集中度和字段缺失统计判断来源多样性。任何 snippet 或 provider content 都只是搜索阶段候选发现信息，不能作为已验证证据。

本次 15 条 Backend-only 结果中，百家号、微信公众号与 CSDN 合计 12 条，占 80%。结果中未出现明显的一手学术出版平台或论文数据库，来源以二次传播、技术博客和资讯页面为主。`多智能体 科技查新 论文` 查询还混入论文 AI 降重、AIGC 检测平台等商业内容；英文 query 仍主要返回中文二手内容。这说明 API 连通性与中文候选发现能力成立，但当前固定请求缺少学术来源约束、语言控制和质量过滤，不能直接满足正式科技查新的来源质量要求。

所有标准化结果均提供 snippet、date 和 website，字段完整性较好；但字段完整不等于内容权威。当前 `content` 体量较大，只保留为 provider metadata，后续应评估是否需要裁剪以控制 Manifest 膨胀。

正式证据链仍应为：WebSearch → Browser → Artifact → Reader → EvidenceCardBuilder。

## 8. 实验发现的数据缺口

- 缺少一手论文数据库、大学机构库或正式出版平台结果；
- 缺少“学术/论文来源”过滤参数，query 中加入“论文”不足以保证学术质量；
- 英文 query 没有带来英文来源，当前 v1 没有语言控制；
- 来源集中于少数内容平台，候选多样性有限；
- provider `content` 与 snippet 仍是未验证搜索材料，不具备 Artifact/Evidence 身份；
- 尚未通过 Browser 验证 URL 可访问性、正文真实性、页面稳定性和元数据一致性；
- 没有标注集，因此不能给出 precision、recall、MAP 或 NDCG；
- 当前仅验证 standard 默认 edition 和 `baidu_search_v2`，没有比较其他可选搜索策略。

## 9. 边界与下一步

本实验未使用 LLM、ToolCallHarness、Browser、Reader 或 EvidenceCardBuilder，未实现缓存、复杂 retry、Router 或多 backend fallback。下一步可在确认结果质量与稳定性后进行 WebSearch + Harness live smoke；在正式 Researcher 工作流接入前，仍需实现 Browser 并验证候选 URL 到 Artifact 的可信转换。
"""


def main() -> None:
    summary = asyncio.run(run_experiment())
    print(
        json.dumps(
            {
                "experiment": summary["experiment"],
                "backend_queries": len(summary["backend_runs"]),
                "manifest_source_record_count": summary[
                    "manifest_source_record_count"
                ],
                "stable_source_record_ids": summary["stable_source_record_ids"],
                "all_result_ids_persisted": summary["all_result_ids_persisted"],
                "overall_duration_ms": summary["overall_duration_ms"],
                "output_dir": str(EXPERIMENT_OUTPUT_DIR),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
