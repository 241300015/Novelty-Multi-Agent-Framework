"""Physical request limits isolate stages and concurrent tasks, not whole runs."""
import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context

import httpx
import pytest
from pydantic import ValidationError

from novelty_agent_framework.config.schemas import RuntimeDebugSettingsConfig
from novelty_agent_framework.core.runtime_artifacts import (
    ProviderPhysicalBudgetExceeded, RuntimeArtifactManager, RuntimeDebugConfig,
    provider_request_budget_task,
)
from novelty_agent_framework.tools.database_search.providers.common import (
    HttpRequestPolicy, ResilientHttpClient,
)


def manager(tmp_path, **kwargs):
    return RuntimeArtifactManager('paper', config=RuntimeDebugConfig(
        output_root=tmp_path/'output', archive_root=tmp_path/'archive',
        max_physical_provider_requests=1, **kwargs))


def reserve(m):
    return m.reserve_provider_request(provider='arxiv', operation='search')


def test_exhausted_research_does_not_block_validator_or_next_round(tmp_path):
    m = manager(tmp_path, provider_request_limits_by_stage={'validate_evidence': 2})
    calls = []
    client = httpx.Client(transport=httpx.MockTransport(
        lambda request: calls.append(request) or httpx.Response(200)))
    transport = ResilientHttpClient(client, policy=HttpRequestPolicy(max_retries=0))
    m.activate()
    try:
        for name, allowance in [('run_research_task', 1), ('validate_evidence', 2),
                                 ('validate_evidence', 2), ('run_research_task', 1)]:
            stage = m.start_stage(name, {})
            try:
                for _ in range(allowance):
                    transport.get('https://example.test/')
                with pytest.raises(ProviderPhysicalBudgetExceeded):
                    transport.get('https://example.test/')
            finally:
                m.finish_stage(stage, {})
    finally:
        m.deactivate()
        client.close()
    assert len(calls) == 6
    m.finish_run('SUCCESS')
    manifest = json.loads((m.run_dir/'manifest.json').read_text())
    assert manifest['physical_provider_requests_reserved'] == 6
    assert [b['used'] for b in manifest['provider_request_budgets']] == [1, 2, 2, 1]
    assert manifest['provider_request_budget_scope'] == 'stage_invocation_task'


def test_parallel_research_invocations_have_independent_limits(tmp_path):
    m = manager(tmp_path)

    async def task():
        stage = m.start_stage('run_research_task', {})
        try:
            await asyncio.sleep(0)
            await asyncio.to_thread(reserve, m)
            with pytest.raises(ProviderPhysicalBudgetExceeded):
                await asyncio.to_thread(reserve, m)
        finally:
            m.finish_stage(stage, {})

    async def run():
        await asyncio.gather(task(), task(), task())

    asyncio.run(run())
    assert len(list((m.run_dir/'provider_budget').glob('*.json'))) == 3


def test_parallel_review_children_are_independent_but_same_child_cannot_reset(tmp_path):
    m = manager(tmp_path)
    stage = m.start_stage('review_evidence', {})

    async def child(name):
        with provider_request_budget_task(name):
            await asyncio.sleep(0)
            await asyncio.to_thread(reserve, m)
        with provider_request_budget_task(name):
            with pytest.raises(ProviderPhysicalBudgetExceeded):
                reserve(m)

    async def run():
        await asyncio.gather(child('card:A'), child('card:B'))

    try:
        asyncio.run(run())
    finally:
        m.finish_stage(stage, {})
    assert len(list((m.run_dir/'provider_budget').glob('*.json'))) == 2


def test_concurrent_requests_inside_one_task_cannot_exceed_limit(tmp_path):
    m = manager(tmp_path)
    stage = m.start_stage('run_research_task', {})

    def attempt():
        try:
            reserve(m)
            return True
        except ProviderPhysicalBudgetExceeded:
            return False

    try:
        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = [pool.submit(copy_context().run, attempt) for _ in range(8)]
            assert sum(f.result() for f in futures) == 1
    finally:
        m.finish_stage(stage, {})


@pytest.mark.parametrize('value', [0, -1, True, '5'])
def test_stage_limits_reject_invalid_values(value):
    with pytest.raises(ValidationError):
        RuntimeDebugSettingsConfig(provider_request_limits_by_stage={'validate_evidence': value})
    with pytest.raises(ValueError):
        RuntimeDebugConfig(provider_request_limits_by_stage={'validate_evidence': value})
