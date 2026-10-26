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
        assert result['artifact_ids'] and not result['full_text_artifact_ids']
        args = DatabaseSearchArguments(source_id='demo', full_text_source_record_ids=[result['source_record_id']])
        acquired = await tool.ainvoke(args, scope=scope())
        assert acquired.succeeded
        assert not acquired.payload['search_executions']
        assert acquired.payload['execution_summary']['operation'] == 'full_text_acquisition'
        assert acquired.payload['acquisition_results'][0]['status'] == 'acquired'
        assert acquired.payload['database_search_result']['results'][0]['full_text_artifact_ids']
        repeated = await tool.ainvoke(args, scope=scope())
        assert repeated.payload['artifacts'] == acquired.payload['artifacts']
        assert repeated.payload['acquisition_results'][0]['status'] == 'reused_verified'
        with pytest.raises(ValueError, match='invalid database'):
            await tool.ainvoke(DatabaseSearchArguments(source_id='demo', full_text_source_record_ids=['invented']), scope=scope())
    asyncio.run(run())
    assert calls == ['paper-1']
    assert all(r.source_kind.value == 'structured_database' for r in store.load_manifest('subject-1').source_records)


def test_empty_fulltext_is_a_failed_acquisition_not_a_search_no_match(tmp_path):
    tool, _, _ = build_tool(tmp_path)
    retrieval = tool.tools_by_source['demo']
    retrieval.full_text_limit = 0

    class EmptyFullTexts:
        source_id = 'demo'

        async def fetch(self, document_id):
            return None

    from dataclasses import replace
    retrieval.source = replace(retrieval.source, full_text_tool=EmptyFullTexts())

    async def run():
        discovery = await tool.ainvoke(DatabaseSearchArguments(source_id='demo'), scope=scope())
        record_id = discovery.payload['database_search_result']['results'][0]['source_record_id']
        acquired = await tool.ainvoke(DatabaseSearchArguments(
            source_id='demo', full_text_source_record_ids=[record_id]), scope=scope())
        assert not acquired.succeeded
        assert acquired.payload['acquisition_results'] == [{
            'source_record_id': record_id,
            'status': 'empty',
            'full_text_artifact_ids': [],
            'failure_event_ids': [acquired.payload['execution_issues'][0]['event_id']],
            'failure_codes': ['retrieval.fulltext.empty'],
        }]
        assert acquired.payload['execution_summary']['no_execution'] is False
        assert acquired.payload['execution_summary']['all_failed'] is True
        assert '全文获取失败' in acquired.summary

    asyncio.run(run())


def test_partial_acquisition_reports_each_record_and_only_verified_fulltext(tmp_path):
    tool, _, _ = build_tool(tmp_path)
    retrieval = tool.tools_by_source['demo']
    retrieval.full_text_limit = 0

    class TwoHits:
        source_id = 'demo'

        def search(self, query, *, limit=10):
            return [
                SearchHit(document_id='one', external_id='one', source_id='demo',
                          title='One', abstract='one abstract'),
                SearchHit(document_id='two', external_id='two', source_id='demo',
                          title='Two', abstract='two abstract'),
            ]

    class PartialFullTexts:
        source_id = 'demo'

        async def fetch(self, document_id):
            if document_id == 'one':
                return FullText(document_id='one', title='One', text='verified full text')
            return None

    from dataclasses import replace
    retrieval.source = replace(
        retrieval.source, search_tool=TwoHits(), full_text_tool=PartialFullTexts())

    async def run():
        discovery = await tool.ainvoke(DatabaseSearchArguments(source_id='demo'), scope=scope())
        ids = [row['source_record_id']
               for row in discovery.payload['database_search_result']['results']]
        acquired = await tool.ainvoke(DatabaseSearchArguments(
            source_id='demo', full_text_source_record_ids=ids), scope=scope())
        assert acquired.succeeded
        assert acquired.payload['execution_summary']['partial'] is True
        assert acquired.payload['execution_summary']['degraded'] is True
        assert [row['status'] for row in acquired.payload['acquisition_results']] == [
            'acquired', 'empty']
        assert len(acquired.payload['acquisition_results'][0]['full_text_artifact_ids']) == 1
        assert acquired.payload['acquisition_results'][1]['full_text_artifact_ids'] == []

    asyncio.run(run())


def test_corrupt_existing_fulltext_is_reacquired_and_not_reused(tmp_path):
    tool, store, _ = build_tool(tmp_path)
    calls = []
    retrieval = tool.tools_by_source['demo']
    retrieval.full_text_limit = 0

    class TrackingFullTexts:
        source_id = 'demo'

        async def fetch(self, document_id):
            calls.append(document_id)
            return FullText(document_id=document_id, title='Paper', text='restored body')

    from dataclasses import replace
    retrieval.source = replace(retrieval.source, full_text_tool=TrackingFullTexts())

    async def run():
        discovery = await tool.ainvoke(DatabaseSearchArguments(source_id='demo'), scope=scope())
        record_id = discovery.payload['database_search_result']['results'][0]['source_record_id']
        args = DatabaseSearchArguments(
            source_id='demo', full_text_source_record_ids=[record_id])
        first = await tool.ainvoke(args, scope=scope())
        artifact_id = first.payload['acquisition_results'][0]['full_text_artifact_ids'][0]
        artifact = next(item for item in store.load_manifest('subject-1').artifacts
                        if item.artifact_id == artifact_id)
        (tmp_path / 'subject-1/references' / artifact.relative_path).write_text('corrupt')
        second = await tool.ainvoke(args, scope=scope())
        assert second.succeeded
        assert second.payload['acquisition_results'][0]['status'] == 'acquired'
        assert any(item['code'].startswith('reader.') or item['code'].startswith('material.')
                   for item in second.payload['execution_issues'])

    asyncio.run(run())
    assert calls == ['paper-1', 'paper-1']


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
