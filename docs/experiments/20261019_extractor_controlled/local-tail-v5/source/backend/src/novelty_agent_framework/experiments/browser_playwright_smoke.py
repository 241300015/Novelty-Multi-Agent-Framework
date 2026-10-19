"""Live Playwright Browser -> Artifact -> Reader experiment (no LLM)."""

from __future__ import annotations

import asyncio
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from ..persistence import ReferenceStore
from ..schemas import (
    AccessStatus,
    BrowserArguments,
    NoveltyPoint,
    ReaderArguments,
    ResearchTask,
    SourceKind,
    SourceRecord,
    TaskResearchRequest,
    WebSearchArguments,
)
from ..tools import (
    BaiduSearchBackend,
    BrowserTool,
    PlaywrightBrowserBackend,
    ReaderTool,
    ReferenceArtifactReaderTool,
    WebSearchTool,
)
from ._support import minimal_search_plan

ROOT = Path(__file__).resolve().parents[4]
OUTPUT_DIR = ROOT / "outputs/experiments/browser-playwright-smoke"
REPORT_PATH = ROOT / "docs/experiments/browser-playwright-smoke/report.md"
SUBJECT_ID = "EXP_BROWSER_PLAYWRIGHT"


class MeasuredPlaywrightBackend(PlaywrightBrowserBackend):
    def __init__(self) -> None:
        super().__init__(navigation_timeout_ms=30_000)
        self.calls: list[dict[str, Any]] = []

    async def fetch(self, url: str):
        started = time.perf_counter()
        try:
            result = await super().fetch(url)
        except Exception as exc:
            self.calls.append(
                {
                    "requested_url": url,
                    "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
            raise
        self.calls.append(
            {
                "requested_url": result.requested_url,
                "final_url": result.final_url,
                "title": result.title,
                "html_length": len(result.html),
                "text_length": len(result.text),
                "content_type": result.content_type,
                "warnings": result.warnings,
                "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
            }
        )
        return result


def _scope() -> TaskResearchRequest:
    return TaskResearchRequest(
        subject_paper_id=SUBJECT_ID,
        run_id="browser-playwright-smoke",
        novelty_point=NoveltyPoint(
            point_id="NP-browser", claim="Browser acquisition", technical_features=["Playwright"]
        ),
        research_task=ResearchTask(
            task_id="TASK-browser",
            novelty_point_id="NP-browser",
            task_type="browser_smoke",
            language="en",
        ),
        search_plan=minimal_search_plan("TASK-browser", "NP-browser"),
    )


def _seed(store: ReferenceStore, source_id: str, title: str, url: str) -> str:
    manifest = store.load_manifest(SUBJECT_ID)
    records = {item.source_record_id: item for item in manifest.source_records}
    current = records.get(source_id)
    if current is None:
        record = SourceRecord(
            source_record_id=source_id,
            source_id="experiment",
            source_kind=SourceKind.WEB,
            title=title,
            landing_url=url,
            access_status=AccessStatus.DISCOVERED,
            observed_at=datetime.now(timezone.utc),
            provenance={"experiment": "browser-playwright-smoke"},
        )
    else:
        record = current.model_copy(
            update={"title": title, "landing_url": url}
        )
    records[source_id] = record
    store.persist_manifest(
        SUBJECT_ID,
        manifest.model_copy(
            update={
                "source_records": list(records.values()),
                "updated_at": datetime.now(timezone.utc),
            }
        ),
    )
    return source_id


async def _browse_and_read(
    label: str,
    source_record_id: str,
    browser: BrowserTool,
    reader: ReaderTool,
    measured: MeasuredPlaywrightBackend,
) -> dict[str, Any]:
    before = len(measured.calls)
    started = time.perf_counter()
    try:
        observation = await browser.ainvoke(
            BrowserArguments(source_record_id=source_record_id), scope=_scope()
        )
        browser_result = observation.payload["browser_result"]
        artifact_id = browser_result["artifacts"][0]["artifact_id"]
        reader_observation = await reader.ainvoke(
            ReaderArguments(artifact_id=artifact_id, char_start=0, max_chars=2_000),
            scope=_scope(),
        )
        return {
            "label": label,
            "status": "success",
            "source_record_id": source_record_id,
            "browser_result": browser_result,
            "fetch": measured.calls[before],
            "reader_result": reader_observation.payload["read_result"],
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
        }
    except Exception as exc:
        fetch = measured.calls[before] if len(measured.calls) > before else None
        return {
            "label": label,
            "status": "failed",
            "source_record_id": source_record_id,
            "fetch": fetch,
            "error": f"{type(exc).__name__}: {exc}",
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
        }


async def run() -> dict[str, Any]:
    load_dotenv(ROOT / "backend/.env")
    store = ReferenceStore(ROOT / "outputs")
    measured = MeasuredPlaywrightBackend()
    browser = BrowserTool(measured, store)
    reader = ReaderTool(ReferenceArtifactReaderTool(store))
    static_id = _seed(
        store, "src_smoke_static", "Example Domain", "https://example.com/"
    )
    rendered_id = _seed(
        store,
        "src_smoke_rendered",
        "Quotes to Scrape JS",
        "https://quotes.toscrape.com/js/",
    )
    cases = [
        await _browse_and_read("public_static", static_id, browser, reader, measured),
        await _browse_and_read("javascript_rendered", rendered_id, browser, reader, measured),
    ]

    search_case: dict[str, Any]
    if not os.getenv("BAIDU_QIANFAN_API_KEY"):
        search_case = {
            "label": "websearch_source",
            "status": "failed",
            "error": "BAIDU_QIANFAN_API_KEY is not configured",
        }
    else:
        search_observation = await WebSearchTool(
            BaiduSearchBackend(), store
        ).ainvoke(
            WebSearchArguments(query="Python asyncio 官方文档", max_results=5),
            scope=_scope(),
        )
        candidates = search_observation.payload["search_result"]["results"]
        if not candidates:
            search_case = {
                "label": "websearch_source",
                "status": "failed",
                "error": "WebSearch returned no candidate",
            }
        else:
            search_case = await _browse_and_read(
                "websearch_source",
                candidates[0]["source_record_id"],
                browser,
                reader,
                measured,
            )
            search_case["search_candidate"] = candidates[0]
    cases.append(search_case)
    manifest = store.load_manifest(SUBJECT_ID)
    result = {
        "experiment": "browser-playwright-smoke",
        "status": "success" if cases[0]["status"] == "success" else "failed",
        "playwright_version": _playwright_version(),
        "cases": cases,
        "successful_cases": sum(case["status"] == "success" for case in cases),
        "failed_cases": sum(case["status"] != "success" for case in cases),
    }
    _write(result, manifest.model_dump(mode="json"))
    return result


def _playwright_version() -> str:
    from importlib.metadata import version

    return version("playwright")


def _write(result: dict[str, Any], manifest: dict[str, Any]) -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    browser_results = [
        {
            key: value
            for key, value in case.items()
            if key not in {"reader_result"}
        }
        for case in result["cases"]
    ]
    reader_results = [
        {"label": case["label"], "reader_result": case.get("reader_result")}
        for case in result["cases"]
    ]
    for name, payload in (
        ("browser_result.json", browser_results),
        ("reader_result.json", reader_results),
        ("manifest_snapshot.json", manifest),
    ):
        (OUTPUT_DIR / name).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    rows = []
    for case in result["cases"]:
        fetch = case.get("fetch") or {}
        browser_result = case.get("browser_result") or {}
        artifacts = browser_result.get("artifacts") or [{}]
        rows.append(
            "| {label} | {status} | {requested} | {final} | {html} | {text} | "
            "{work} | {artifact} | {reader} | {elapsed} | {warnings} |".format(
                label=case["label"],
                status=case["status"],
                requested=fetch.get("requested_url", "—"),
                final=fetch.get("final_url", "—"),
                html=fetch.get("html_length", "—"),
                text=fetch.get("text_length", "—"),
                work=browser_result.get("work_id", "—"),
                artifact=artifacts[0].get("artifact_id", "—"),
                reader="yes" if case.get("reader_result") else "no",
                elapsed=case.get("elapsed_ms", "—"),
                warnings="; ".join(fetch.get("warnings", [])) or case.get("error", "—"),
            )
        )
    report = f"""# Browser Playwright 真实实验报告

Playwright {result['playwright_version']}；仅安装 Chromium。本实验不使用 LLM。

运行环境数据缺口：`playwright install chromium` 只下载浏览器二进制，宿主最初缺少 NSPR、NSS、ALSA 动态库且没有免密 sudo；本次改为在 Novelty Conda 环境安装 `nspr`、`nss`、`alsa-lib`，运行时通过该环境的 library path 启动。部署文档仍需明确系统依赖安装步骤。

| case | status | requested_url | final_url | HTML 长度 | text 长度 | Work ID | Artifact ID | Reader | 耗时 ms | warnings/error |
|---|---|---|---|---:|---:|---|---|---|---:|---|
{chr(10).join(rows)}

成功 {result['successful_cases']} 项，失败 {result['failed_cases']} 项。WebSearch 候选页面遇到反爬、跳转或导航失败时按真实失败记录，不伪造成功。Reader 仅通过 Browser 返回的 `artifact_id` 读取持久化文本，没有复制 Browser 正文作为输入。
"""
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(report, encoding="utf-8")


def main() -> None:
    print(json.dumps(asyncio.run(run()), ensure_ascii=False, indent=2))
    print(f"outputs: {OUTPUT_DIR}")


if __name__ == "__main__":
    main()
