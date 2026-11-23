import asyncio

import pytest

from backend.env import ModelCallOptions, ModelResponse
from novelty_agent_framework.agents.evidence_reviewer import _BudgetedClient
from novelty_agent_framework.core.format_repair import repair_json
from novelty_agent_framework.ports import FullText, SearchHit
from novelty_agent_framework.schemas import DatabaseSearchArguments
from test_database_search_tool import build_tool, scope


def test_discover_then_acquire_without_research_and_reuse_artifact(tmp_path):
    tool, store, _ = build_tool(tmp_path)
    retrieval = tool.tools_by_source['demo']
    retrieval.full_text_limit = 0
    calls = []
    class FullTexts:
        source_id = 'demo'
        async def fetch(self, document_id):
            calls.append(document_id)
            return FullText(document_id=document_id, title='Paper', text='Original body', content_extent='full')
    # RetrievalSource is immutable; replace its capability for this isolated test.
    from dataclasses import replace
    retrieval.source = replace(retrieval.source, full_text_tool=FullTexts())
    async def run():
        discovery = await tool.ainvoke(DatabaseSearchArguments(source_id='demo'), scope=scope())
        assert not calls
        result = discovery.payload['database_search_result']['results'][0]
        assert not result['artifact_ids'] and not result['full_text_artifact_ids']
        record = next(r for r in store.load_manifest(scope().subject_paper_id).source_records
                      if r.source_record_id == result['source_record_id'])
        assert record.abstract and result['abstract_preview'] == record.abstract[:500]
        args = DatabaseSearchArguments(source_id='demo', full_text_source_record_ids=[result['source_record_id']])
        acquired = await tool.ainvoke(args, scope=scope())
        assert acquired.succeeded
        assert not acquired.payload['search_executions']
        assert acquired.payload['database_search_result']['results'][0]['full_text_artifact_ids']
        repeated = await tool.ainvoke(args, scope=scope())
        assert repeated.payload['artifacts'] == acquired.payload['artifacts']
        with pytest.raises(ValueError, match='invalid database'):
            await tool.ainvoke(DatabaseSearchArguments(source_id='demo', full_text_source_record_ids=['invented']), scope=scope())
    asyncio.run(run())
    assert calls == ['paper-1']
    assert all(r.source_kind.value == 'structured_database' for r in store.load_manifest('subject-1').source_records)


def test_parallel_acquisition_shares_limit_and_duplicate_request(tmp_path):
    tool, _, _ = build_tool(tmp_path)
    retrieval = tool.tools_by_source['demo']
    retrieval.max_concurrency = 1
    active = peak = 0
    calls = []
    class FullTexts:
        source_id = 'demo'
        async def fetch(self, document_id):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            calls.append(document_id)
            await asyncio.sleep(.02)
            active -= 1
            return FullText(document_id=document_id, title='Paper', text='body')
    from dataclasses import replace
    retrieval.source = replace(retrieval.source, full_text_tool=FullTexts())
    async def run():
        await asyncio.gather(*(retrieval._fetch_full_texts([(doc, SearchHit(document_id=doc, title='Paper'))])
                              for doc in ['same', 'same', 'other']))
    asyncio.run(run())
    assert peak == 1
    assert sorted(calls) == ['other', 'same']


def test_repair_disables_thinking_and_bounds_transport_timeout():
    class Client:
        async def acomplete(self, messages, *, options):
            assert options.extra_body['enable_thinking'] is False
            assert options.timeout_seconds == 60
            assert options.tools == ()
            return ModelResponse(content='{}')
    asyncio.run(repair_json(Client(), '{}', {}, ModelCallOptions(timeout_seconds=600, extra_body={'enable_thinking': True})))


def test_review_transport_timeout_uses_remaining_card_budget():
    timeouts = []
    class Client:
        async def acomplete(self, messages, *, options):
            timeouts.append(options.timeout_seconds)
            await asyncio.sleep(.02)
            return ModelResponse(content='{}')
    async def run():
        client = _BudgetedClient(Client(), 1)
        await client.acomplete([], options=ModelCallOptions(timeout_seconds=600))
        await client.acomplete([], options=ModelCallOptions(timeout_seconds=600))
    asyncio.run(run())
    assert 0 < timeouts[1] < timeouts[0] <= 1
