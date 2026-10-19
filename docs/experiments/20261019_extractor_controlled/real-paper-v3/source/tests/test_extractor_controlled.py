"""Behavioral boundaries of the experimental Extractor choice-only Harness."""
import asyncio
import json

import pytest

from backend.env import ModelResponse
from novelty_agent_framework.agents.point_extractor import NoveltyPointExtractorAgent, build_paper_digest
from novelty_agent_framework.core.extractor_harness import ExtractorHarnessConfig, ExtractorHarnessError
from novelty_agent_framework.core.paper_reading import PaperReader, PaperReadingError
from novelty_agent_framework.persistence import persist_workflow_input
from novelty_agent_framework.schemas import PaperInput


class Client:
    def __init__(self, handler):
        self.handler, self.calls, self.systems = handler, [], []

    def complete(self, messages, *, options=None):
        text = messages[-1].content
        context = json.loads(text[text.index('{'):])
        self.calls.append(context)
        self.systems.append(messages[0].content)
        result = self.handler(context)
        if isinstance(result, Exception):
            raise result
        return ModelResponse(content=result if isinstance(result, str) else json.dumps(result))


def point(context, claim='事件触发调度机制'):
    return dict(claim=claim, technical_features=['根据事件动态更新调度队列'],
                source_locations=[context['reads'][-1]['source_location']])


def read(context, *, all=False):
    options = context['options']
    selected = next((o for o in options if o['kind'] == 'read_all'), options[0]) if all else next(o for o in reversed(options) if o['kind'] == 'read_segment')
    return {'option_ids': [selected['option_id']], 'reason': '读取后段方法贡献'}


def passed(context):
    return {'point_reviews': [
        {'point_id': p['point_id'], 'status': 'pass', 'issues': []} for p in context['points']],
        'missing_contributions': [], 'read_requests': [], 'comparisons': [
            {'option_id': o['option_id'], 'relation': 'distinct', 'reason': '不同技术机制',
             'source_locations': [context['reads'][-1]['source_location']]}
            for o in context.get('comparison_options', [])]}


def rejected(context):
    result = passed(context)
    result['point_reviews'][-1] = {'point_id': context['points'][-1]['point_id'], 'status': 'revise',
        'issues': [{'code': 'duplicate', 'reason': '同一机制的改写',
                    'related_point_ids': [context['points'][0]['point_id']],
                    'source_locations': [context['reads'][-1]['source_location']]}]}
    result['comparisons'][0].update(relation='duplicate', reason='同一机制的改写')
    return result


def setup(tmp_path, handler, **limits):
    paper = PaperInput(paper_id='controlled', title='Methods', abstract='Background only',
        full_text='Background. ' * 900 + ' LATE-METHOD: event triggered scheduling.')
    workspace, ref = persist_workflow_input(paper, output_root=tmp_path, with_reading_reference=True)
    client = Client(handler)
    agent = NoveltyPointExtractorAgent(client, harness_config=ExtractorHarnessConfig(enabled=True, **limits))
    digest = build_paper_digest(paper).model_copy(update={'paper_source': ref})
    return agent, client, digest, workspace


def baseline(context):
    if context['phase'] == 'reading_scope':
        return read(context)
    if context['phase'] == 'generate':
        return {'option_id': context['option_id'], 'points': [point(context)]}
    return passed(context)


def test_one_point_passes_without_quantity_padding(tmp_path):
    agent, client, digest, _ = setup(tmp_path, baseline)
    result = agent.extract(digest, previous_brief=None, attempt=1)
    assert len(result) == 1
    assert [c['phase'] for c in client.calls] == ['reading_scope', 'generate', 'semantic_review']
    assert client.systems[1] != client.systems[2]
    assert 'LATE-METHOD' in client.calls[1]['reads'][-1]['text']
    assert agent.last_trace['semantic_review_passed'] is True
    assert agent.last_trace['coverage_complete'] is False


def test_semantic_repair_uses_issued_patch_and_rechecks(tmp_path):
    def handler(ctx):
        if ctx['phase'] == 'generate':
            return {'option_id': ctx['option_id'], 'points': [point(ctx), point(ctx, '通过事件触发进行调度')]}
        if ctx['phase'] == 'semantic_review' and len(ctx['points']) == 2:
            return rejected(ctx)
        if ctx['phase'] == 'repair':
            assert '同一机制的改写' in ctx['correction_procedure']
            assert all(o['target_ids'][0] == 'NP-2' for o in ctx['options'])
            option = next(o for o in ctx['options'] if o['action'] == 'withdraw')
            return {'repairs': [{'option_id': option['option_id'], 'points': []}]}
        return baseline(ctx)
    agent, client, digest, _ = setup(tmp_path, handler)
    result = agent.extract(digest, previous_brief=None, attempt=1)
    assert [p.point_id for p in result] == ['NP-1']
    assert agent.last_trace['versions'][0]['points'][0] == result[0].model_dump(mode='json')
    assert agent.last_trace['budgets']['used'] == {'generate': 2, 'review': 2, 'read': 1}
    assert [c['phase'] for c in client.calls][-2:] == ['repair', 'semantic_review']


@pytest.mark.parametrize('fault', ['missing_pair', 'contradictory_pair', 'uncertain_pass'])
def test_pair_review_cannot_silently_bypass_comparison(tmp_path, fault):
    def handler(ctx):
        if ctx['phase'] == 'generate':
            return {'option_id': ctx['option_id'], 'points': [point(ctx), point(ctx, '另一项')]}
        if ctx['phase'] == 'semantic_review':
            result = passed(ctx)
            if fault == 'missing_pair':
                result['comparisons'] = []
            else:
                result['comparisons'][0]['relation'] = 'duplicate' if fault == 'contradictory_pair' else 'uncertain'
            return result
        return baseline(ctx)
    agent, _, digest, _ = setup(tmp_path, handler)
    with pytest.raises(ExtractorHarnessError):
        agent.extract(digest, previous_brief=None, attempt=1)
    assert agent.last_trace['approved_points'] == []
    assert agent.last_trace['budgets']['used']['review'] == 2


def test_unissued_comparison_feedback_removes_placeholder(tmp_path):
    first = True
    def handler(ctx):
        nonlocal first
        result = baseline(ctx)
        if ctx['phase'] == 'semantic_review':
            if first:
                first = False
                result['comparisons'] = [{'option_id': '', 'source_locations': []}]
            else:
                assert 'Set comparisons to [] exactly' in ctx['correction']['error']
        return result
    agent, _, digest, _ = setup(tmp_path, handler)
    assert agent.extract(digest, previous_brief=None, attempt=1)
    assert agent.last_trace['corrections'][0]['status'] == 'verified'


@pytest.mark.parametrize('phase,role', [('generate', 'generate'), ('semantic_review', 'review'), ('reading_scope', 'read')])
def test_legality_correction_charges_responsible_role(tmp_path, phase, role):
    bad = False
    def handler(ctx):
        nonlocal bad
        if ctx['phase'] == phase and not bad:
            bad = True
            return 'broken json'
        if ctx['phase'] == phase:
            assert ctx['correction']['failed_output'] == 'broken json'
            assert '只处理本次反馈' in ctx['correction']['procedure']
        return baseline(ctx)
    agent, _, digest, _ = setup(tmp_path, handler)
    assert agent.extract(digest, previous_brief=None, attempt=1)
    assert agent.last_trace['budgets']['used'][role] == 2
    assert all(v == (2 if k == role else 1) for k, v in agent.last_trace['budgets']['used'].items())
    assert agent.last_trace['corrections'][0]['status'] == 'verified'


def test_unissued_reader_choice_cannot_reach_generation(tmp_path):
    agent, client, digest, _ = setup(tmp_path, lambda ctx: {'option_ids': ['../../paper.pdf'], 'reason': 'escape'})
    with pytest.raises(ExtractorHarnessError):
        agent.extract(digest, previous_brief=None, attempt=1)
    assert len(client.calls) == 2
    assert not agent.last_trace['reading']['generation_allowed']
    assert agent.last_trace['budgets']['used']['generate'] == 0


def test_patch_cannot_modify_passed_candidate(tmp_path):
    def handler(ctx):
        if ctx['phase'] == 'generate':
            return {'option_id': ctx['option_id'], 'points': [point(ctx), point(ctx, '改写')]}
        if ctx['phase'] == 'semantic_review': return rejected(ctx)
        if ctx['phase'] == 'repair':
            oid = ctx['options'][0]['option_id'].replace('NP-2', 'NP-1')
            return {'repairs': [{'option_id': oid, 'points': [point(ctx, '擅自改写已通过项')]}]}
        return baseline(ctx)
    agent, _, digest, _ = setup(tmp_path, handler)
    with pytest.raises(ExtractorHarnessError): agent.extract(digest, previous_brief=None, attempt=1)
    assert len(agent.last_trace['versions']) == 1
    assert agent.last_trace['approved_points'][0]['claim'] == '事件触发调度机制'


def test_missing_snapshot_stops_without_pdf_fallback(tmp_path):
    agent, client, digest, workspace = setup(tmp_path, baseline)
    (workspace / digest.paper_source.index_path).with_name('paper.json').unlink()
    with pytest.raises(PaperReadingError): agent.extract(digest, previous_brief=None, attempt=1)
    assert not client.calls


def test_reader_reverifies_snapshot_between_selection_and_read(tmp_path):
    agent, _, digest, workspace = setup(tmp_path, baseline)
    path = (workspace / digest.paper_source.index_path).with_name('paper.json')
    def damage(ctx):
        path.write_text('{}')
        return read(ctx)
    agent.model_client = Client(damage)
    with pytest.raises(PaperReadingError, match='hash_mismatch'):
        agent.extract(digest, previous_brief=None, attempt=1)
    assert agent.last_trace['budgets']['used']['generate'] == 0


def test_review_can_request_only_issued_unread_segments(tmp_path):
    requested = False
    def handler(ctx):
        nonlocal requested
        if ctx['phase'] == 'semantic_review' and not requested:
            requested = True
            return {'point_reviews': [], 'missing_contributions': [],
                    'read_requests': [{'option_id': ctx['reader_options'][0]['option_id'], 'reason': '核查前文'}]}
        return baseline(ctx)
    agent, client, digest, _ = setup(tmp_path, handler)
    assert agent.extract(digest, previous_brief=None, attempt=1)
    assert agent.last_trace['budgets']['used'] == {'generate': 1, 'review': 2, 'read': 1}
    assert len(client.calls[-1]['reads']) == 2


def test_review_budget_exhaustion_returns_only_approved_subset(tmp_path):
    def handler(ctx):
        if ctx['phase'] == 'generate':
            return {'option_id': ctx['option_id'], 'points': [point(ctx), point(ctx, '重复')]}
        if ctx['phase'] == 'semantic_review': return rejected(ctx)
        return baseline(ctx)
    agent, _, digest, _ = setup(tmp_path, handler, review_calls=1)
    result = agent.extract(digest, previous_brief=None, attempt=1)
    assert [p.point_id for p in result] == ['NP-1']
    assert agent.last_trace['execution_status'] == 'partial'
    assert agent.last_trace['unresolved_points'][0]['point_id'] == 'NP-2'
    assert agent.last_trace['budgets']['used']['generate'] == 1


def test_workflow_persists_controlled_trace_without_runtime(tmp_path):
    from novelty_agent_framework.workflows import NoveltyWorkflow
    workflow = NoveltyWorkflow.default(output_root=tmp_path)
    workflow.point_extractor = NoveltyPointExtractorAgent(Client(baseline), harness_config={'enabled': True})
    paper = PaperInput(paper_id='flow', title='Flow', full_text='A method')
    result = asyncio.run(workflow._extract_points({'paper': paper}))
    trace = json.loads((tmp_path / 'flow/point_extraction_trace.json').read_text())
    assert trace['harness'] == 'extractor_choices_v1'
    assert trace['final_points'] == [p.model_dump(mode='json') for p in result['novelty_points']]


def test_experiment_profile_reaches_factory_and_prompt_snapshot(tmp_path):
    from novelty_agent_framework.config import load_application_config, build_workflow
    from novelty_agent_framework.config.experiment import freeze_config
    config = load_application_config(environ={}, profile_path='config/profiles/extractor-harness-local.json')
    workflow = build_workflow(config, output_root=tmp_path)
    assert workflow.point_extractor.harness_config.enabled
    prompts = freeze_config(config)['selected_prompts']['point_extractor']
    assert set(prompts) == {'generate', 'semantic_review', 'read', 'correct'}
    assert all(p['exists'] for p in prompts.values())


def test_missing_contribution_addition_is_reviewed_with_retained_points(tmp_path):
    first_review = True
    def handler(ctx):
        nonlocal first_review
        if ctx['phase'] == 'semantic_review' and first_review:
            first_review = False
            result = passed(ctx)
            result['missing_contributions'] = [{'reason': '原文还给出独立压缩机制',
                'source_locations': [ctx['reads'][-1]['source_location']]}]
            return result
        if ctx['phase'] == 'repair':
            option = ctx['options'][0]
            assert option['action'] == 'add' and option['target_ids'] == []
            return {'repairs': [{'option_id': option['option_id'], 'points': [point(ctx, '独立压缩机制')]}]}
        return baseline(ctx)
    agent, client, digest, _ = setup(tmp_path, handler)
    result = agent.extract(digest, previous_brief=None, attempt=1)
    assert len(result) == 2
    assert len(client.calls[-1]['points']) == 2
    assert agent.last_trace['reviews'][-1]['version'] == 2


def test_reader_selection_cannot_overrun_combined_char_budget(tmp_path):
    def handler(ctx):
        return {'option_ids': [o['option_id'] for o in ctx['options']], 'reason': 'attempt all'}
    agent, client, digest, _ = setup(tmp_path, handler, max_read_chars=4000)
    with pytest.raises(ExtractorHarnessError): agent.extract(digest, previous_brief=None, attempt=1)
    assert agent.last_trace['budgets']['reader_attempts'] == 0
    assert len(client.calls) == 2


def test_generated_fake_source_requires_legality_repair(tmp_path):
    first = True
    def handler(ctx):
        nonlocal first
        result = baseline(ctx)
        if ctx['phase'] == 'generate' and first:
            first = False
            result['points'][0]['source_locations'] = ['paper:other:full_text[0:1]']
        return result
    agent, _, digest, _ = setup(tmp_path, handler)
    assert agent.extract(digest, previous_brief=None, attempt=1)
    assert agent.last_trace['events'][0]['code'] == 'model.output_reference'
    assert agent.last_trace['budgets']['used']['generate'] == 2


def test_merge_invalidates_previously_passed_representative(tmp_path):
    def handler(ctx):
        if ctx['phase'] == 'generate':
            return {'option_id': ctx['option_id'], 'points': [point(ctx), point(ctx, '另一表述')]}
        if ctx['phase'] == 'semantic_review' and len(ctx['points']) == 2:
            return rejected(ctx)
        if ctx['phase'] == 'repair':
            option = next(o for o in ctx['options'] if o['action'] == 'merge')
            return {'repairs': [{'option_id': option['option_id'], 'points': [point(ctx, '合并后的事件触发机制')]}]}
        return baseline(ctx)
    agent, client, digest, _ = setup(tmp_path, handler)
    result = agent.extract(digest, previous_brief=None, attempt=1)
    assert result[0].point_id == 'NP-1'
    assert client.calls[-1]['points'][0]['claim'] == '合并后的事件触发机制'
    assert len(agent.last_trace['reviews']) == 2


def test_repeated_noop_correction_stops_and_keeps_passed_point(tmp_path):
    def handler(ctx):
        if ctx['phase'] == 'generate':
            return {'option_id': ctx['option_id'], 'points': [point(ctx), point(ctx, '另一表述')]}
        if ctx['phase'] == 'semantic_review': return rejected(ctx)
        if ctx['phase'] == 'repair':
            option = next(o for o in ctx['options'] if o['action'] == 'revise')
            return {'repairs': [{'option_id': option['option_id'], 'points': [point(ctx, '另一表述')]}]}
        return baseline(ctx)
    agent, _, digest, _ = setup(tmp_path, handler)
    with pytest.raises(ExtractorHarnessError): agent.extract(digest, previous_brief=None, attempt=1)
    assert len(agent.last_trace['versions']) == 1
    assert agent.last_trace['approved_points'][0]['point_id'] == 'NP-1'


def test_reader_io_retry_keeps_version_and_counts_attempts(tmp_path, monkeypatch):
    from novelty_agent_framework.schemas.failures import FailureCode
    original = PaperReader.read
    count = 0
    def flaky(reader, segment):
        nonlocal count
        count += 1
        if count == 1:
            raise PaperReadingError(FailureCode.READER_VERIFICATION_FAILED, 'transient I/O')
        return original(reader, segment)
    monkeypatch.setattr(PaperReader, 'read', flaky)
    agent, _, digest, _ = setup(tmp_path, baseline)
    assert agent.extract(digest, previous_brief=None, attempt=1)
    assert agent.last_trace['budgets']['reader_attempts'] == 2
    assert agent.last_trace['reading']['reference'] == digest.paper_source.model_dump(mode='json')


def test_no_approved_result_on_review_transport_failure(tmp_path):
    from backend.env.model_client import ModelClientError
    def handler(ctx):
        return ModelClientError('test transport failure') if ctx['phase'] == 'semantic_review' else baseline(ctx)
    agent, _, digest, _ = setup(tmp_path, handler)
    with pytest.raises(ExtractorHarnessError): agent.extract(digest, previous_brief=None, attempt=1)
    assert agent.last_trace['approved_points'] == []
    assert len(agent.last_trace['unresolved_points']) == 1


def test_unfounded_addition_can_be_sent_back_for_review_without_fabrication(tmp_path):
    first = True
    def handler(ctx):
        nonlocal first
        if ctx['phase'] == 'semantic_review' and first:
            first = False
            result = passed(ctx)
            result['missing_contributions'] = [{'reason': '背景信息遗漏',
                'source_locations': [ctx['reads'][-1]['source_location']]}]
            return result
        if ctx['phase'] == 'repair':
            option = next(o for o in ctx['options'] if o['action'] == 'request_recheck')
            return {'repairs': [{'option_id': option['option_id'], 'points': [],
                                'reason': '背景不是作者提出的独立机制，请复核原文。'}]}
        if ctx['phase'] == 'semantic_review':
            assert ctx['generation_feedback'][0]['reason'].startswith('背景不是')
        return baseline(ctx)
    agent, _, digest, _ = setup(tmp_path, handler)
    result = agent.extract(digest, previous_brief=None, attempt=1)
    assert len(result) == 1
    assert len(agent.last_trace['versions']) == 1
    assert len(agent.last_trace['reviews']) == 2
    assert agent.last_trace['budgets']['used']['generate'] == 2


def test_recheck_request_never_self_approves_missing_contribution(tmp_path):
    def handler(ctx):
        if ctx['phase'] == 'semantic_review':
            result = passed(ctx)
            result['missing_contributions'] = [{'reason': '仍需核查',
                'source_locations': [ctx['reads'][-1]['source_location']]}]
            return result
        if ctx['phase'] == 'repair':
            option = next(o for o in ctx['options'] if o['action'] == 'request_recheck')
            return {'repairs': [{'option_id': option['option_id'], 'points': [], 'reason': '没有独立机制'}]}
        return baseline(ctx)
    agent, _, digest, _ = setup(tmp_path, handler, review_calls=2)
    assert agent.extract(digest, previous_brief=None, attempt=1)
    assert agent.last_trace['execution_status'] == 'partial'
    assert agent.last_trace['semantic_review_passed'] is False
