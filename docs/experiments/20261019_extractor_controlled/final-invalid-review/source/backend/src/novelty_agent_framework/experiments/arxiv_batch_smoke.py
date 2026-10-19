"""A/B/D/C live smoke for the process-wide arXiv scheduler experiment."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from typing import Any

from .arxiv_rate_smoke import DEFAULT_CASES, probe_case
from ..tools.database_search.providers.arxiv import (
    ArxivMetadataTool,
    ArxivSearchTool,
)
from ..tools.database_search.providers.arxiv_scheduler import ArxivRequestScheduler


KNOWN_METADATA_IDS = (
    "1706.03762",
    "1810.04805",
    "2005.14165",
    "2103.00020",
    "2203.02155",
    "2302.13971",
    "2307.09288",
    "2310.06825",
    "2403.08295",
    "2407.21783",
)


def run_batch_smoke(
    *,
    scheduler: ArxivRequestScheduler,
) -> dict[str, Any]:
    search = ArxivSearchTool(scheduler=scheduler)
    metadata = ArxivMetadataTool(scheduler=scheduler)
    a = probe_case(search, DEFAULT_CASES[0])
    b = (
        probe_case(search, DEFAULT_CASES[1])
        if a["classification"] == "VALID_HIT"
        else _skipped("B", "exact_english_title")
    )
    d: dict[str, Any]
    if b["classification"] == "VALID_HIT":
        try:
            with ThreadPoolExecutor(max_workers=len(KNOWN_METADATA_IDS)) as executor:
                futures = [executor.submit(copy_context().run, metadata.resolve, doc_id) for doc_id in KNOWN_METADATA_IDS]
                values = [future.result() for future in futures]
            resolved = sum(value is not None for value in values)
            d = {
                "case": "D",
                "purpose": "metadata_batch",
                "requested_ids": list(KNOWN_METADATA_IDS),
                "resolved_count": resolved,
                "classification": (
                    "VALID_HIT" if resolved == len(KNOWN_METADATA_IDS) else "PARTIAL_MISSING"
                ),
            }
        except Exception as exc:
            d = {
                "case": "D",
                "purpose": "metadata_batch",
                "requested_ids": list(KNOWN_METADATA_IDS),
                "classification": "PROVIDER_ACCESS_FAILURE",
                "error_type": type(exc).__name__,
                "error": str(exc),
            }
    else:
        d = _skipped("D", "metadata_batch")
    c = (
        probe_case(search, DEFAULT_CASES[2])
        if d["classification"] == "VALID_HIT"
        else _skipped("C", "run_d_structured_query")
    )
    cases = [a, b, d, c]
    return {
        "check": "ARXIV-BATCH-01",
        "provider_access": (
            "PASS"
            if all(item["classification"] == "VALID_HIT" for item in (a, b, d))
            else "FAIL"
        ),
        "execution_order": ["A", "B", "D", "C"],
        "cases": cases,
        "scheduler_metrics": scheduler.snapshot_metrics(include_events=True),
    }


def _skipped(case_id: str, purpose: str) -> dict[str, Any]:
    return {
        "case": case_id,
        "purpose": purpose,
        "classification": "SKIPPED_UPSTREAM_GATE",
    }


__all__ = ["KNOWN_METADATA_IDS", "run_batch_smoke"]


WEB_THEME_QUERIES = (
    'ti:"graph neural network"',
    'ti:"music structure"',
    'ti:"attention"',
    'ti:"time series"',
    'ti:"contrastive learning"',
    'ti:"image classification"',
    'ti:"language model"',
    'ti:"molecular representation"',
)


def run_web_smoke(*, session, queries=WEB_THEME_QUERIES, max_concurrency=1):
    """Web counterpart of the existing provider smoke; no model calls."""
    import time
    from dataclasses import asdict
    from ..tools.database_search.providers.arxiv_web import ArxivWebSearchTool
    from ..tools.database_search.providers.arxiv_scheduler import provider_task_id

    search = ArxivWebSearchTool(session=session)
    def probe(item):
        index, query = item
        token = provider_task_id.set(f"web-smoke-{index + 1}")
        started = time.monotonic()
        try:
            hits = list(search.search(query, limit=5))
            return {"query": query, "classification": "VALID_HIT" if hits else "ZERO_RESULT",
                    "hits": [asdict(hit) for hit in hits], "elapsed_seconds": time.monotonic() - started}
        except Exception as exc:
            return {"query": query, "classification": "PROVIDER_FAILED",
                    "error_type": type(exc).__name__, "error": str(exc),
                    "elapsed_seconds": time.monotonic() - started}
        finally:
            provider_task_id.reset(token)
    if max_concurrency == 1:
        cases = [probe(item) for item in enumerate(queries)]
    else:
        with ThreadPoolExecutor(max_workers=max_concurrency) as executor:
            futures = [executor.submit(copy_context().run, probe, item) for item in enumerate(queries)]
            cases = [future.result() for future in futures]
    return {"transport": "web", "max_concurrency": max_concurrency,
            "cases": cases, "scheduler_metrics": session.stats()}


def run_api_parallel_smoke(*, scheduler):
    """Four concurrent logical calls, including metadata, even on a blocked exit."""
    from ..tools.database_search.providers.arxiv_scheduler import provider_task_id
    search = ArxivSearchTool(scheduler=scheduler)
    def probe(index):
        token = provider_task_id.set(f"api-smoke-{index + 1}")
        try:
            if index < 2:
                value = scheduler.resolve_metadata(KNOWN_METADATA_IDS[index])
                count = int(value is not None)
            else:
                count = len(search.search(WEB_THEME_QUERIES[index], limit=3))
            return {"task_id": provider_task_id.get(), "classification": "VALID_HIT" if count else "ZERO_RESULT", "count": count}
        except Exception as exc:
            return {"task_id": provider_task_id.get(), "classification": "PROVIDER_FAILED", "error_type": type(exc).__name__, "error": str(exc)}
        finally:
            provider_task_id.reset(token)
    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = [executor.submit(copy_context().run, probe, index) for index in range(4)]
        cases = [future.result() for future in futures]
    return {"max_concurrency": 4, "cases": cases, "scheduler_metrics": scheduler.snapshot_metrics(include_events=True)}
