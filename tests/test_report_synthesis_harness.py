"""Per-point isolation, asynchronous barrier, field repair and durable outcomes."""
import asyncio
import json

import pytest

from backend.env import ModelResponse, ModelTransportTimeout
from novelty_agent_framework.agents import NoveltyCoordinatorAgent
from novelty_agent_framework.core.report_synthesis import CoordinatorSynthesisHarness, ReportSynthesisError
from novelty_agent_framework.schemas import NoveltyBrief, PaperInput
from novelty_agent_framework.schemas.domain import PointNarrativeDraft
from test_report_narrative_contract import _case


def inputs(count=3):
    points, cards, reviews, _ = _case(count)
    return dict(paper=PaperInput(paper_id='synthesis', title='Paper', abstract='Subject context', full_text='Subject body'),
        brief=NoveltyBrief(paper_summary='Overview', novelty_points=points),
        evidence=cards, novelty_reviews=reviews)


def result(summary='Related work uses mechanism A; the current point differs in condition B.'):
    return ModelResponse(content=json.dumps({'summary': summary}))


def batch(tmp_path):
    return json.loads(next(tmp_path.glob('synthesis/report-synthesis/*.json')).read_text())


def test_concurrency_barrier_order_and_point_scope(tmp_path):
    async def scenario():
        started, done, active, peak = [], [], 0, 0
        two_started, release_second, release_first = asyncio.Event(), asyncio.Event(), asyncio.Event()
        async def synthesize(materials, *, schema, repair_feedback):
            nonlocal active, peak
            point = materials['novelty_point']['point_id']
            index = int(point.split('-')[-1])
            assert [c['novelty_point_id'] for c in materials['cards']] == [point]
            assert materials['reviewer_result']['novelty_point_id'] == point
            assert set(schema['properties']) == {'summary'}
            assert set(schema['required']) == {'summary'}
            assert 'supporting_card_ids' not in json.dumps(schema)
            started.append(point)
            active += 1
            peak = max(peak, active)
            if len(started) == 2:
                two_started.set()
            if index == 0:
                await release_first.wait()
            elif index == 1:
                await release_second.wait()
            done.append(point)
            active -= 1
            return result(f'综述：当前材料针对方法 {index} 的机制和适用范围提供比较依据。')
        harness = CoordinatorSynthesisHarness(synthesize, max_concurrency=2)
        data = inputs()
        task = asyncio.create_task(harness.run(**data, output_root=tmp_path, run_id='run-one'))
        await asyncio.wait_for(two_started.wait(), 2)
        assert peak == 2 and len(started) == 2
        assert not list(tmp_path.rglob('report-synthesis/*.json'))
        release_second.set()
        while len(done) < 2:
            await asyncio.sleep(0)
        assert done == ['P-1', 'P-2']
        assert not task.done() and not list(tmp_path.rglob('report-synthesis/*.json'))
        release_first.set()
        report = await task
        assert [c.novelty_point_id for c in report.conclusions] == ['P-0', 'P-1', 'P-2']
        assert report.conclusions[0].summary.startswith('综述')
        assert all(not c.supporting_card_ids and not c.counter_card_ids for c in report.conclusions)
        assert report.conclusions[0].verdict_reason == data['novelty_reviews'][0].verdict_reason
        saved = batch(tmp_path)
        assert saved['barrier_complete'] and saved['status'] == 'completed'
        assert [r['point_id'] for r in saved['point_results']] == ['P-0', 'P-1', 'P-2']
        assert saved['report'] == report.model_dump(mode='json')
    asyncio.run(scenario())


def test_failed_point_waits_for_siblings_and_persists_all_outcomes(tmp_path):
    finished = []
    async def synthesize(materials, **kwargs):
        point = materials['novelty_point']['point_id']
        if point == 'P-0':
            raise ModelTransportTimeout('injected')
        await asyncio.sleep(0)
        finished.append(point)
        return result()
    with pytest.raises(ReportSynthesisError, match='response_unavailable') as caught:
        asyncio.run(CoordinatorSynthesisHarness(synthesize).run(**inputs(), output_root=tmp_path))
    assert set(finished) == {'P-1', 'P-2'}
    saved = batch(tmp_path)
    assert saved['status'] == 'failed' and saved['barrier_complete']
    assert [r['status'] for r in saved['point_results']] == ['failed', 'completed', 'completed']
    assert caught.value.artifact_path.is_file()
    assert 'report' not in saved and not list(tmp_path.rglob('report.json'))


@pytest.mark.parametrize('violation', ['no_progress', 'scope', 'stale'])
def test_field_repair_feedback_is_bounded_and_preserves_program_identity(tmp_path, violation):
    feedbacks = []
    async def synthesize(materials, *, schema, repair_feedback):
        if repair_feedback is None:
            return result('')
        feedbacks.append(repair_feedback)
        option = next(iter(repair_feedback['options']))
        if len(feedbacks) == 1:
            updates = {} if violation == 'no_progress' else {'point_id': 'P-99'}
            if violation == 'stale':
                option, updates = 'not-issued', {}
        else:
            assert set(feedbacks[0]['options']).isdisjoint(repair_feedback['options'])
            assert feedbacks[0]['fields'] == repair_feedback['fields']
            updates = {key: '有效综述：相关工作与本点的技术差异及适用条件。' for key in repair_feedback['fields']}
        return ModelResponse(content=json.dumps({'option_id': option, 'updates': updates}))
    report = asyncio.run(CoordinatorSynthesisHarness(synthesize).run(**inputs(1), output_root=tmp_path))
    assert report.conclusions[0].novelty_point_id == 'P-0'
    row = batch(tmp_path)['point_results'][0]
    assert len(row['attempts']) == 3
    assert row['repair_audit']['changed_paths'] == [['summary']]
    assert row['repair_audit']['revalidation'] == 'passed'


def test_extra_binding_fields_can_only_be_removed_without_rewriting_valid_summary(tmp_path):
    original = '已有文献采用机制 A，本点改变了适用条件，具体差异仍受证据范围限制。'
    async def synthesize(materials, *, schema, repair_feedback):
        if repair_feedback is None:
            return ModelResponse(content=json.dumps({'summary': original, 'supporting_card_ids': ['foreign']}))
        assert [f['path'] for f in repair_feedback['fields'].values()] == [['supporting_card_ids']]
        assert all(f['operation'] == 'remove' for f in repair_feedback['fields'].values())
        return ModelResponse(content=json.dumps({'option_id': next(iter(repair_feedback['options'])),
            'updates': {key: None for key in repair_feedback['fields']}}))
    report = asyncio.run(CoordinatorSynthesisHarness(synthesize).run(**inputs(1), output_root=tmp_path))
    assert report.conclusions[0].summary == original
    assert not report.conclusions[0].supporting_card_ids


@pytest.mark.parametrize('mode,count', [('noop', 3), ('invalid_value', 2), ('stop', 2)])
def test_repair_cannot_loop_or_publish_invalid_result(tmp_path, mode, count):
    calls = []
    async def synthesize(materials, *, schema, repair_feedback):
        calls.append(repair_feedback)
        if repair_feedback is None:
            return result('')
        options = list(repair_feedback['options'])
        updates = {} if mode == 'noop' else {key: 42 for key in repair_feedback['fields']}
        return ModelResponse(content=json.dumps({'option_id': options[-1] if mode == 'stop' else options[0],
            'updates': None if mode == 'stop' else updates}))
    with pytest.raises(ReportSynthesisError):
        asyncio.run(CoordinatorSynthesisHarness(synthesize).run(**inputs(1), output_root=tmp_path))
    assert len(calls) == count
    assert batch(tmp_path)['status'] == 'failed'


def test_actual_async_agent_call_receives_single_point_and_no_binding_schema(tmp_path):
    class Client:
        def complete(self, *args, **kwargs):
            raise AssertionError('production path must call acomplete')
        async def acomplete(self, messages, *, options):
            schema = options.response_format['json_schema']['schema']
            assert set(schema['properties']) == {'summary'}
            content = messages[1].content
            material, _ = json.JSONDecoder().raw_decode(content[content.index('{'):])
            point = material['novelty_point']['point_id']
            assert all(c['novelty_point_id'] == point for c in material['cards'])
            assert '不要逐卡罗列' in messages[0].content
            assert options.tools == () and options.tool_choice == 'none'
            return result()
    data = inputs(2)
    data.update(rejected_evidence=[], insufficient_final_evidence_points=[])
    report = asyncio.run(NoveltyCoordinatorAgent(Client()).asynthesize(**data, output_root=tmp_path))
    assert len(report.conclusions) == 2


def test_cancellation_does_not_leave_background_workers_or_completed_artifact(tmp_path):
    async def scenario():
        started, cancelled = asyncio.Event(), []
        async def synthesize(materials, **kwargs):
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.append(materials['novelty_point']['point_id'])
                raise
        task = asyncio.create_task(CoordinatorSynthesisHarness(synthesize).run(**inputs(2), output_root=tmp_path))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert set(cancelled) == {'P-0', 'P-1'}
        assert not list(tmp_path.rglob('report-synthesis/*.json'))
    asyncio.run(scenario())


def test_schema_has_no_model_owned_identity_or_card_groups():
    schema = PointNarrativeDraft.model_json_schema()
    assert set(schema['properties']) == {'summary'}
    assert schema['additionalProperties'] is False


@pytest.mark.parametrize('mutation', ['duplicate_point', 'missing_review', 'foreign_card', 'foreign_review_card'])
def test_invalid_upstream_binding_is_rejected_before_model_calls(mutation):
    data = inputs(2)
    if mutation == 'duplicate_point':
        data['brief'].novelty_points.append(data['brief'].novelty_points[0])
    elif mutation == 'missing_review':
        data['novelty_reviews'].pop()
    elif mutation == 'foreign_card':
        data['evidence'][0].novelty_point_id = 'foreign'
    else:
        from novelty_agent_framework.schemas import RelevantWork
        data['novelty_reviews'][0].highly_relevant_works = [RelevantWork(work_id='w',
            card_ids=[data['evidence'][1].card_id], evidence_ids=['foreign-evidence'], relevance_reason='wrong scope')]
    async def unexpected(*args, **kwargs):
        pytest.fail('invalid input must not enter synthesis')
    with pytest.raises(ValueError):
        asyncio.run(CoordinatorSynthesisHarness(unexpected).run(**data))


def test_workflow_passes_concurrency_and_persists_report_after_join(tmp_path):
    from dataclasses import replace
    from novelty_agent_framework.workflows import NoveltyWorkflow
    from novelty_agent_framework.persistence import persist_workflow_input
    from test_workflow_reviewer import _workflow

    class Client:
        async def acomplete(self, messages, *, options):
            return result()
    coordinator = NoveltyCoordinatorAgent(Client())
    workflow = NoveltyWorkflow(replace(_workflow().services, coordinator=coordinator), output_root=tmp_path)
    data = inputs(2)
    state = {'paper': data['paper'], 'brief': data['brief'], 'novelty_points': data['brief'].novelty_points,
        'evidence_cards': data['evidence'], 'novelty_reviews': data['novelty_reviews'], 'run_id': 'workflow-run'}
    persist_workflow_input(state['paper'], output_root=tmp_path, run_id=state['run_id'])
    async def scenario():
        state.update(await workflow._synthesize_report(state))
        assert batch(tmp_path)['barrier_complete']
        index = json.loads((tmp_path / 'synthesis/report-inputs.json').read_text())
        assert any(name.startswith('report-synthesis/') for name in index['artifacts'])
        assert not (tmp_path / 'synthesis/report.json').exists()
        await workflow._persist_report(state)
    asyncio.run(scenario())
    saved = json.loads((tmp_path / 'synthesis/report.json').read_text())
    assert [c['novelty_point_id'] for c in saved['conclusions']] == ['P-0', 'P-1']
    assert batch(tmp_path)['max_concurrency'] == workflow.config.max_concurrency


def test_concurrent_runtime_stages_keep_point_scope(tmp_path):
    from novelty_agent_framework.core import RuntimeArtifactManager, RuntimeDebugConfig
    manager = RuntimeArtifactManager('synthesis', config=RuntimeDebugConfig(
        output_root=tmp_path, archive_root=tmp_path / 'archive'))
    manager.activate()
    parent = manager.start_stage('synthesize_report', {})
    async def synthesize(materials, **kwargs):
        await asyncio.sleep(0)
        return result()
    try:
        report = asyncio.run(CoordinatorSynthesisHarness(synthesize).run(**inputs(2), output_root=tmp_path,
            run_id=manager.run_id))
        manager.finish_stage(parent, {'report': report.model_dump(mode='json')})
        children = [json.loads(p.read_text()) for p in manager.run_dir.glob('stages/*synthesize_point/meta.json')]
        assert len(children) == 2
        assert {c['scope']['point_id'] for c in children} == {'P-0', 'P-1'}
        assert all(c['status'] == 'SUCCESS' for c in children)
    finally:
        manager.deactivate()


def test_standalone_synthesis_runs_do_not_overwrite_previous_batch(tmp_path):
    async def synthesize(*args, **kwargs):
        return result()
    for _ in range(2):
        asyncio.run(CoordinatorSynthesisHarness(synthesize).run(**inputs(1), output_root=tmp_path))
    assert len(list(tmp_path.glob('synthesis/report-synthesis/*.json'))) == 2
