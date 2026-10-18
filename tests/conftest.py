"""pytest 全局配置：默认跳过真实应用边界或外部服务的 live 测试。"""

from __future__ import annotations

import os

import pytest

from novelty_agent_framework.schemas import SearchConcept, SearchPlan, SearchStrategy


def minimal_search_plan(task_id: str, novelty_point_id: str) -> SearchPlan:
    """Build the smallest valid plan for tests whose subject is not planning."""

    return SearchPlan(
        task_id=task_id,
        novelty_point_id=novelty_point_id,
        concepts=[SearchConcept(concept_id="C1", name="concept", terms=["term"])],
        strategies=[
            SearchStrategy(strategy_id="S1", level="strict", expression="C1")
        ],
    )


def pytest_collection_modifyitems(config, items) -> None:
    """默认跳过 live 测试，除非显式 ``-m live`` 或设置 ``RUN_LIVE_TESTS=1``。"""

    markexpr = config.getoption("markexpr") or ""
    if "live" in markexpr or os.getenv("RUN_LIVE_TESTS"):
        return
    skip_live = pytest.mark.skip(
        reason="live 测试：需要显式执行 pytest -m live 或设置 RUN_LIVE_TESTS=1"
    )
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip_live)
