"""Behavioral boundaries of the Extractor choice-only Harness."""
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
        self.messages = []

    def complete(self, messages, *, options=None):
        text = messages[-1].content
        context, _ = json.JSONDecoder().raw_decode(text[text.index('{'):])
        self.messages.append(messages)
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


def dedup_choice(context, keep=None):
    option = next((o for o in context['options'] if keep is None or o['point_id'] in keep), context['options'][0])
    return {'option_id': option['option_id']}


def duplicate_pairs(context, pairs):
    result = passed(context)
    rows = {row['point_id']: row for row in result['point_reviews']}
    comparisons = {c['option_id']: c for c in result['comparisons']}
    for a, b in pairs:
        rows[b]['status'] = 'revise'
        rows[b]['issues'].append({'code': 'duplicate', 'reason': '同一机制的两种表述',
            'related_point_ids': [a], 'source_locations': [context['reads'][-1]['source_location']]})
        comparisons[f'compare-{a}-{b}'].update(relation='duplicate')
    return result


def setup(tmp_path, handler, *, paper=None, **limits):
    paper = paper if paper is not None else PaperInput(paper_id='controlled', title='Methods', abstract='Background only',
        full_text='Background. ' * 900 + ' LATE-METHOD: event triggered scheduling.')
    workspace, ref = persist_workflow_input(paper, output_root=tmp_path, with_reading_reference=True)
    client = Client(handler)
    agent = NoveltyPointExtractorAgent(client, harness_config=ExtractorHarnessConfig(enabled=True, **limits))
    digest = build_paper_digest(paper).model_copy(update={'paper_source': ref})
    return agent, client, digest, workspace


def baseline(context):
    if context['phase'] == 'reading_scope':
        return read(context)
    if context['phase'] == 'post_generation_reading':
        option = next(o for o in context['options'] if o['kind'] == 'continue_review')
        return {'option_ids': [option['option_id']], 'reason': '已读材料仅有一项独立贡献，交审核复核覆盖'}
    if context['phase'] == 'generate':
        return {'option_id': context['option_id'], 'points': [point(context)]}
    return passed(context)


def test_one_point_passes_without_quantity_padding(tmp_path):
    agent, client, digest, _ = setup(tmp_path, baseline)
    result = agent.extract(digest, previous_brief=None, attempt=1)
    assert len(result) == 1
    assert [c['phase'] for c in client.calls] == ['reading_scope', 'generate', 'post_generation_reading', 'semantic_review']
    assert client.systems[1] != client.systems[2]
    assert 'LATE-METHOD' in client.calls[1]['reads'][-1]['text']
    assert agent.last_trace['semantic_review_passed'] is True
    assert agent.last_trace['coverage_complete'] is False
    assert agent.last_trace['post_generation_reading']['status'] == 'declined'
    assert agent.last_trace['budgets']['reader_attempts'] == 1


def test_complete_digest_reaches_model_and_trace_including_correction(tmp_path):
    paper = PaperInput(paper_id='digest-contract', title='事件调度',
        abstract='中文摘要', english_abstract='English abstract',
        keywords_zh=['事件调度'], keywords_en=['event scheduling'],
        claimed_contributions=['独立调度机制'],
        full_text='正文导览。' * 900 + '\n主要贡献总结：后段独立调度机制。')
    corrected = False

    def handler(ctx):
        nonlocal corrected
        if ctx['phase'] == 'generate' and not corrected:
            corrected = True
            return 'not JSON'
        return baseline(ctx)

    agent, client, digest, _ = setup(tmp_path, handler, paper=paper)
    before = digest.model_dump(mode='json')
    assert agent.extract(digest, previous_brief=None, attempt=1)
    assert [c['phase'] for c in client.calls] == [
        'reading_scope', 'generate', 'generate', 'post_generation_reading', 'semantic_review']
    assert before['keywords_zh'] and before['keywords_en']
    assert '后段独立调度机制' in before['full_text_excerpt']
    assert len(before['full_text_excerpt']) > 2000
    for context, record in zip(client.calls, agent.last_trace['calls'], strict=True):
        assert context['digest'] == before
        assert record['input']['digest'] == before
    assert digest.model_dump(mode='json') == before
    assert agent.last_trace['budgets']['reader_attempts'] == 1
    assert agent.last_trace['reading']['unread_segments']


def test_deduplication_uses_issued_choice_and_rechecks(tmp_path):
    def handler(ctx):
        if ctx['phase'] == 'generate':
            return {'option_id': ctx['option_id'], 'points': [point(ctx), point(ctx, '通过事件触发进行调度')]}
        if ctx['phase'] == 'semantic_review' and len(ctx['points']) == 2:
            return rejected(ctx)
        if ctx['phase'] == 'deduplicate':
            assert ctx['review']['point_reviews'][-1]['issues'][0]['reason'] == '同一机制的改写'
            assert [o['point_id'] for o in ctx['options']] == ['NP-1', 'NP-2']
            return dedup_choice(ctx, ['NP-1'])
        return baseline(ctx)
    agent, client, digest, _ = setup(tmp_path, handler)
    result = agent.extract(digest, previous_brief=None, attempt=1)
    assert [p.point_id for p in result] == ['NP-1']
    assert agent.last_trace['versions'][0]['points'][0] == result[0].model_dump(mode='json')
    assert agent.last_trace['budgets']['used'] == {'generate': 2, 'review': 2, 'read': 2}
    assert [c['phase'] for c in client.calls][-2:] == ['deduplicate', 'semantic_review']


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


@pytest.mark.parametrize('phase,role', [('generate', 'generate'), ('semantic_review', 'review'),
                                      ('reading_scope', 'read'), ('post_generation_reading', 'read')])
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
    expected = {'generate': 1, 'review': 1, 'read': 2}
    expected[role] += 1
    assert agent.last_trace['budgets']['used'] == expected
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
        if ctx['phase'] == 'deduplicate':
            result = dedup_choice(ctx, ['NP-1'])
            result['points'] = [{**ctx['points'][0], 'claim': '擅自改写已通过项'}]
            return result
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
    assert agent.last_trace['budgets']['used'] == {'generate': 1, 'review': 2, 'read': 2}
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
    assert trace['harness'] == 'extractor_choices_v6'
    assert trace['final_points'] == [p.model_dump(mode='json') for p in result['novelty_points']]
    assert result['point_coverage']['semantic_review_passed'] is True
    assert result['point_coverage']['semantic_completeness_verified'] is False
    assert result['point_coverage']['unread_segments'] == []
    assert result['point_coverage']['read_chars'] == len(paper.full_text)
    assert 'point_coverage_unresolved' in {issue.code for issue in result['issues']}


def test_default_harness_reaches_factory_and_prompt_snapshot(tmp_path):
    from novelty_agent_framework.config import load_application_config, build_workflow
    from novelty_agent_framework.config.experiment import freeze_config
    config = load_application_config(environ={})
    workflow = build_workflow(config, output_root=tmp_path)
    assert workflow.point_extractor.harness_config.enabled
    prompts = freeze_config(config)['selected_prompts']['point_extractor']
    assert set(prompts) == {'generate', 'semantic_review', 'read', 'correct'}
    assert all(p['exists'] for p in prompts.values())


def test_missing_contribution_addition_is_reviewed_with_retained_points(tmp_path):
    first_review = True
    def handler(ctx):
        nonlocal first_review
        if ctx['phase'] == 'post_generation_reading':
            assert ctx['initial_count'] == 1 and ctx['suggested_point_count'] == 3
            return read(ctx)
        if ctx['phase'] == 'semantic_review' and first_review:
            first_review = False
            assert len(ctx['reads']) == 2
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
    assert agent.last_trace['post_generation_reading']['status'] == 'read_completed'
    assert agent.last_trace['reading']['reads'][-1]['origin'] == 'post_generation_reading'
    assert [p.point_id for p in result] == ['NP-1', 'NP-2']
    assert result[0].model_dump(mode='json') == agent.last_trace['versions'][0]['points'][0]
    assert sum(c['phase'] == 'post_generation_reading' for c in client.calls) == 1


def test_three_initial_points_skip_extra_reading_decision(tmp_path):
    def handler(ctx):
        if ctx['phase'] == 'generate':
            return {'option_id': ctx['option_id'], 'points': [point(ctx, f'独立机制 {i}') for i in range(3)]}
        return baseline(ctx)
    agent, client, digest, _ = setup(tmp_path, handler)
    assert len(agent.extract(digest, previous_brief=None, attempt=1)) == 3
    assert [c['phase'] for c in client.calls] == ['reading_scope', 'generate', 'semantic_review']
    assert agent.last_trace['post_generation_reading']['status'] == 'not_needed'


@pytest.mark.parametrize('limit', ['model_calls', 'no_unread_segments'])
def test_short_draft_records_unavailable_extra_reading_and_still_reviews(tmp_path, limit):
    def handler(ctx):
        if limit == 'no_unread_segments' and ctx['phase'] == 'reading_scope':
            return read(ctx, all=True)
        return baseline(ctx)
    agent, client, digest, _ = setup(tmp_path, handler, reading_calls=1 if limit == 'model_calls' else 3)
    assert len(agent.extract(digest, previous_brief=None, attempt=1)) == 1
    assert [c['phase'] for c in client.calls] == ['reading_scope', 'generate', 'semantic_review']
    audit = agent.last_trace['post_generation_reading']
    assert audit['status'] == 'skipped'
    assert audit['reason'] == ('reading_call_budget_exhausted' if limit == 'model_calls'
                               else 'no_readable_segments_within_budget')


def test_unissued_post_generation_read_is_rejected_without_io(tmp_path):
    def handler(ctx):
        if ctx['phase'] == 'post_generation_reading':
            return {'option_ids': ['forged-segment'], 'reason': 'attempt unissued read'}
        return baseline(ctx)
    agent, _, digest, _ = setup(tmp_path, handler)
    with pytest.raises(ExtractorHarnessError):
        agent.extract(digest, previous_brief=None, attempt=1)
    assert agent.last_trace['budgets']['reader_attempts'] == 1
    assert agent.last_trace['budgets']['used'] == {'generate': 1, 'review': 0, 'read': 3}
    assert len(agent.last_trace['versions']) == 1


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


def test_deduplication_can_keep_previously_revised_candidate_and_rechecks(tmp_path):
    def handler(ctx):
        if ctx['phase'] == 'generate':
            return {'option_id': ctx['option_id'], 'points': [point(ctx), point(ctx, '另一表述')]}
        if ctx['phase'] == 'semantic_review' and len(ctx['points']) == 2:
            return rejected(ctx)
        if ctx['phase'] == 'deduplicate':
            return dedup_choice(ctx, ['NP-2'])
        return baseline(ctx)
    agent, client, digest, _ = setup(tmp_path, handler)
    result = agent.extract(digest, previous_brief=None, attempt=1)
    assert result[0].point_id == 'NP-2'
    assert client.calls[-1]['points'][0]['claim'] == '另一表述'
    assert len(agent.last_trace['reviews']) == 2


@pytest.mark.parametrize('kept', ['NP-2', 'NP-3'])
def test_deduplication_returns_complete_four_to_three_list(tmp_path, kept):
    from novelty_agent_framework.core.extractor_harness import ExtractorHarness
    expected = ['NP-1', kept, 'NP-4']
    def handler(ctx):
        if ctx['phase'] == 'generate':
            return {'option_id': ctx['option_id'], 'points': [point(ctx, f'机制 {n}') for n in range(1, 5)]}
        if ctx['phase'] == 'semantic_review' and len(ctx['points']) == 4:
            return duplicate_pairs(ctx, [('NP-2', 'NP-3')])
        if ctx['phase'] == 'deduplicate':
            assert [o['point_id'] for o in ctx['options']] == ['NP-2', 'NP-3']
            assert ctx['duplicate_point_ids'] == ['NP-2', 'NP-3']
            assert all(set(o) == {'option_id', 'point_id', 'point'} for o in ctx['options'])
            return dedup_choice(ctx, expected)
        return baseline(ctx)
    agent, client, digest, _ = setup(tmp_path, handler)
    result = agent.extract(digest, previous_brief=None, attempt=1)
    assert [p.point_id for p in result] == expected
    original = {p['point_id']: p for p in agent.last_trace['versions'][0]['points']}
    assert [p.model_dump(mode='json') for p in result] == [original[pid] for pid in expected]
    assert [p['point_id'] for p in client.calls[-1]['points']] == expected
    assert agent.last_trace['corrections'][0]['removed_point_ids'] == [({'NP-2', 'NP-3'} - {kept}).pop()]
    correction = agent.last_trace['corrections'][0]
    assert correction['result_points'] == [original[pid] for pid in expected]
    assert set(correction['response']) == {'option_id'}
    # The model can select only an issued plan; it cannot supply list contents.
    ctx = next(c for c in client.calls if c['phase'] == 'deduplicate')
    schema = ExtractorHarness(client)._response_schema('generate', ctx)
    assert schema['required'] == ['option_id']
    assert set(schema['properties']) == {'option_id'}
    assert schema['additionalProperties'] is False
    assert schema['properties']['option_id']['enum'] == [o['option_id'] for o in ctx['options']]


@pytest.mark.parametrize('fault', [
    'missing_option', 'multiple_choices', 'nonstring', 'unrelated_point',
    'stale_version', 'foreign_run', 'injected_members', 'extra_reason',
])
def test_deduplication_rejects_invalid_choices_transactionally(tmp_path, fault):
    def handler(ctx):
        if ctx['phase'] == 'generate':
            return {'option_id': ctx['option_id'], 'points': [point(ctx, f'机制 {n}') for n in range(1, 5)]}
        if ctx['phase'] == 'semantic_review':
            return duplicate_pairs(ctx, [('NP-2', 'NP-3')])
        if ctx['phase'] == 'deduplicate':
            response = dedup_choice(ctx, ['NP-1', 'NP-3', 'NP-4'])
            if fault == 'missing_option': response = {}
            elif fault == 'multiple_choices': response['option_id'] = [o['option_id'] for o in ctx['options']]
            elif fault == 'nonstring': response['option_id'] = 3
            elif fault == 'unrelated_point': response['option_id'] = response['option_id'].rsplit(':retain:', 1)[0] + ':retain:NP-4'
            elif fault == 'stale_version': response['option_id'] = response['option_id'].split(':v', 1)[0] + ':v0:retain:NP-3'
            elif fault == 'foreign_run': response['option_id'] = 'foreign:' + response['option_id']
            elif fault == 'injected_members': response['keep_point_ids'] = ['NP-3']
            elif fault == 'extra_reason': response['reason'] = '绕过选项协议'
            return response
        return baseline(ctx)
    agent, _, digest, _ = setup(tmp_path, handler)
    with pytest.raises(ExtractorHarnessError, match='invalid output persisted'):
        agent.extract(digest, previous_brief=None, attempt=1)
    assert len(agent.last_trace['versions']) == 1
    assert len(agent.last_trace['versions'][0]['points']) == 4
    assert agent.last_trace['budgets']['used']['generate'] == 3
    assert agent.last_trace['corrections'][-1]['status'] == 'stopped'


def test_deduplication_corrected_choice_applies_once_and_rechecks(tmp_path):
    def handler(ctx):
        if ctx['phase'] == 'generate':
            return {'option_id': ctx['option_id'], 'points': [point(ctx), point(ctx, '详细表述')]}
        if ctx['phase'] == 'semantic_review' and len(ctx['points']) == 2:
            return rejected(ctx)
        if ctx['phase'] == 'deduplicate':
            if 'correction' not in ctx:
                return {'option_id': 'unissued'}
            assert 'currently issued option_id' in ctx['correction']['error']
            return dedup_choice(ctx, ['NP-2'])
        return baseline(ctx)
    agent, client, digest, _ = setup(tmp_path, handler)
    result = agent.extract(digest, previous_brief=None, attempt=1)
    assert [p.point_id for p in result] == ['NP-2']
    assert len(agent.last_trace['versions']) == 2
    assert agent.last_trace['corrections'][0]['status'] == 'verified'
    assert sum(c.get('kind') == 'deduplication' for c in agent.last_trace['corrections']) == 1
    assert agent.last_trace['budgets']['used'] == {'generate': 3, 'review': 2, 'read': 2}
    assert client.calls[-1]['phase'] == 'semantic_review'


@pytest.mark.parametrize('pairs,kept', [
    ([('NP-1', 'NP-2'), ('NP-3', 'NP-4')], ['NP-2', 'NP-3']),
    ([('NP-1', 'NP-2'), ('NP-1', 'NP-3'), ('NP-2', 'NP-3')], ['NP-3', 'NP-4']),
    ([('NP-1', 'NP-2'), ('NP-2', 'NP-3')], ['NP-1', 'NP-3', 'NP-4']),
])
def test_deduplication_handles_multiple_pairs_without_deleting_unrelated_points(tmp_path, pairs, kept):
    def handler(ctx):
        if ctx['phase'] == 'generate':
            return {'option_id': ctx['option_id'], 'points': [point(ctx, f'机制 {n}') for n in range(1, 5)]}
        if ctx['phase'] == 'semantic_review':
            ids = {p['point_id'] for p in ctx['points']}
            remaining = [(a, b) for a, b in pairs if a in ids and b in ids]
            return duplicate_pairs(ctx, remaining)
        if ctx['phase'] == 'deduplicate':
            assert len(ctx['options']) == 2
            return dedup_choice(ctx, kept)
        return baseline(ctx)
    agent, _, digest, _ = setup(tmp_path, handler, generation_calls=4, review_calls=4)
    result = agent.extract(digest, previous_brief=None, attempt=1)
    assert [p.point_id for p in result] == kept
    changes = [c for c in agent.last_trace['corrections'] if c.get('kind') == 'deduplication']
    assert len(changes) == 4 - len(kept)
    assert len(agent.last_trace['reviews']) == len(changes) + 1
    for before, after in zip(agent.last_trace['versions'], agent.last_trace['versions'][1:]):
        assert len(after['points']) == len(before['points']) - 1


def test_repeated_noop_correction_stops_and_keeps_passed_point(tmp_path):
    def handler(ctx):
        if ctx['phase'] == 'generate':
            return {'option_id': ctx['option_id'], 'points': [point(ctx), point(ctx, '另一表述')]}
        if ctx['phase'] == 'semantic_review':
            result = rejected(ctx)
            result['point_reviews'][-1]['issues'][0].update(code='scope', reason='范围表述有误')
            result['comparisons'][0].update(relation='distinct')
            return result
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


@pytest.mark.parametrize('retry_each_segment', [False, True])
def test_dynamic_budget_allows_long_full_text_with_retry_allowance(tmp_path, monkeypatch, retry_each_segment):
    from novelty_agent_framework.schemas.failures import FailureCode
    paper = PaperInput(paper_id='long-budget', title='Long paper', full_text='原文' * 52001)
    attempts = {}
    original = PaperReader.read

    def flaky(reader, segment):
        attempts[segment] = attempts.get(segment, 0) + 1
        if retry_each_segment and attempts[segment] == 1:
            raise PaperReadingError(FailureCode.READER_VERIFICATION_FAILED, 'transient I/O')
        return original(reader, segment)

    monkeypatch.setattr(PaperReader, 'read', flaky)

    def handler(ctx):
        if ctx['phase'] == 'reading_scope':
            assert len(ctx['content_list']) > 24
            assert ctx['read_budget'] == {
                'chars': 2 * len(paper.full_text), 'segments': 2 * len(ctx['content_list'])}
            assert any(o['kind'] == 'read_all' for o in ctx['options'])
            return read(ctx, all=True)
        if ctx['phase'] == 'generate':
            assert ''.join(r['text'] for r in ctx['reads']) == paper.full_text
        return baseline(ctx)

    agent, _, digest, _ = setup(tmp_path, handler, paper=paper)
    assert agent.extract(digest, previous_brief=None, attempt=1)
    budget = agent.last_trace['budgets']
    assert budget['char_limit'] == 2 * len(paper.full_text)
    assert budget['read_chars'] == len(paper.full_text)
    assert budget['read_attempt_chars'] == len(paper.full_text) * (2 if retry_each_segment else 1)
    assert budget['reader_attempts'] == len(attempts) * (2 if retry_each_segment else 1)
    assert budget['reader_limit'] == 2 * len(attempts)
    assert budget['char_limit_mode'] == 'twice_full_text'
    assert agent.last_trace['reading']['unread_segments'] == []


def test_retry_cannot_exceed_explicit_character_limit(tmp_path, monkeypatch):
    from novelty_agent_framework.schemas.failures import FailureCode
    attempts = []
    paper = PaperInput(paper_id='retry-budget', title='Retry budget', full_text='12345')

    def failed_read(reader, segment):
        attempts.append(segment)
        raise PaperReadingError(FailureCode.READER_VERIFICATION_FAILED, 'transient I/O')

    monkeypatch.setattr(PaperReader, 'read', failed_read)
    agent, client, digest, _ = setup(tmp_path, baseline, paper=paper, max_read_chars=9)
    with pytest.raises(ExtractorHarnessError, match='Reader execution budget exhausted'):
        agent.extract(digest, previous_brief=None, attempt=1)
    assert attempts == ['body-1']
    assert [c['phase'] for c in client.calls] == ['reading_scope']
    budget = agent.last_trace['budgets']
    assert budget['read_attempt_chars'] == 5
    assert budget['read_chars'] == 0
    assert budget['char_limit'] == 9
    assert budget['char_limit_mode'] == 'fixed'


def test_shortest_full_text_uses_exact_character_budget(tmp_path):
    paper = PaperInput(paper_id='short-budget', title='Short', full_text='文')
    agent, client, digest, _ = setup(tmp_path, baseline, paper=paper)
    assert agent.extract(digest, previous_brief=None, attempt=1)
    assert client.calls[0]['read_budget'] == {'chars': 2, 'segments': 2}
    assert agent.last_trace['budgets']['char_limit'] == 2
    assert agent.last_trace['budgets']['reader_limit'] == 2
    assert agent.last_trace['budgets']['read_attempt_chars'] == 1


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
