from copy import deepcopy

import pytest

from test_search_planner import make_point, make_task
from novelty_agent_framework.core.concept_evidence import build_catalog, resolve_evidence
from novelty_agent_framework.core.concept_review_v2 import (
    EvidenceReview, review_payload, to_internal_review, validate_evidence_review,
)


def evidence_fixture():
    point, task = make_point(), make_task()
    catalog = build_catalog(point, task)
    payload = review_payload(dict(point=point.model_dump(), task=task.model_dump(),
        source_catalog=catalog, comparison=[], check_coverage=True,
        candidates={'1': dict(role='method', importance=3, terms=['图摘要'], alias=['摘要图'])}))
    result = EvidenceReview.model_validate(dict(decisions=[dict(candidate_id='C1',
        support_status='supported', evidence_ids=[catalog['sources'][0]['source_id']],
        source_reason='输入支持', role_correct=True, role_reason='分类合理',
        translation_consistent=None, translation_reason='',
        terms=[dict(term_id='C1.T1', substitutable=True, reason='相同含义')],
        aliases=[dict(alias_id='C1.A1', substitutable=True, reason='别名等义')])], missing=[]))
    return point, task, payload, result


def test_catalog_reversible_unicode_repeated_and_stable():
    point = make_point().model_copy(update={'claim': '第一句😀。第一句😀。\n末尾',
        'claim_en': 'Dr. Smith uses 2.5 units. Next sentence.', 'technical_features': ['长句' * 1000, ''],
        'technical_features_en': []})
    task = make_task()
    catalog = build_catalog(point, task)
    assert catalog == build_catalog(point, task)
    assert len({s['source_id'] for s in catalog['sources']}) == len(catalog['sources'])
    for s in catalog['sources']:
        raw = getattr(point, s['field'])
        if s['field_index'] is not None:
            raw = raw[s['field_index']]
        assert raw[s['start']:s['end']] == s['text']
    assert [s['text'] for s in catalog['sources'] if s['field'] == 'claim_en'] == [
        'Dr. Smith uses 2.5 units.', ' Next sentence.']
    assert not any(s['field'] == 'source_locations' for s in catalog['sources'])


def test_resolution_rejects_cross_task_tampering_and_fake_ids():
    point, task, payload, _ = evidence_fixture()
    catalog = payload['source_catalog']
    sid = catalog['sources'][0]['source_id']
    for ids in [['S-fake'], [sid, sid]]:
        with pytest.raises(ValueError, match='evidence.id'):
            resolve_evidence(catalog, ids, point, task)
    with pytest.raises(ValueError, match='evidence.binding'):
        resolve_evidence(catalog, [sid], point, task.model_copy(update={'task_id': 'other'}))
    changed = deepcopy(catalog)
    changed['sources'][0]['text'] = '篡改'
    with pytest.raises(ValueError, match='evidence.integrity'):
        resolve_evidence(changed, [sid], point, task)


@pytest.mark.parametrize('corruption', ['fake_id', 'wrong_term', 'wrong_alias', 'no_evidence', 'wrong_candidate', 'translation'])
def test_review_rejects_wrong_id_and_field_coverage(corruption):
    _, _, payload, result = evidence_fixture()
    d = result.decisions[0]
    if corruption == 'fake_id': d.evidence_ids = ['S-fake']
    elif corruption == 'wrong_term': d.terms[0].term_id = 'C1.A1'
    elif corruption == 'wrong_alias': d.aliases = []
    elif corruption == 'no_evidence': d.evidence_ids = []
    elif corruption == 'wrong_candidate': d.candidate_id = 'C2'
    elif corruption == 'translation': d.translation_consistent = True
    with pytest.raises(ValueError):
        validate_evidence_review(result, payload)


def test_ids_resolve_to_program_owned_quote_and_uncertain_is_not_passed():
    _, _, payload, result = evidence_fixture()
    internal = to_internal_review(result, payload)
    assert internal.decisions[0].source_quote == payload['source_catalog']['sources'][0]['text']
    assert internal.decisions[0].terms[0].term == '图摘要'
    result.decisions[0].support_status = 'uncertain'
    result.decisions[0].evidence_ids = []
    assert not to_internal_review(result, payload).decisions[0].source_supported


def test_local_review_requires_resolution_and_forbids_missing():
    _, _, payload, result = evidence_fixture()
    payload['check_coverage'] = False
    with pytest.raises(ValueError, match='review.resolution'):
        validate_evidence_review(result, payload)
    result.decisions[0].issue_resolution = 'unresolved'
    result.decisions[0].resolution_reason = '依据不足'
    validate_evidence_review(result, payload)
    result.missing = ['越界新增']
    with pytest.raises(ValueError, match='review.scope'):
        validate_evidence_review(result, payload)


@pytest.mark.parametrize('async_mode', [False, True])
def test_v2_full_planner_rejects_foreign_id_then_recovers(tmp_path, monkeypatch, async_mode):
    import asyncio
    import json
    from test_search_planner import SearchPlannerAgent, StubModelClient, valid_draft
    class AsyncStub(StubModelClient):
        async def acomplete(self, messages, *, options=None):
            return self.complete(messages, options=options)
    monkeypatch.chdir(tmp_path)
    point, task = make_point(), make_task()
    draft = valid_draft(task)
    catalog = build_catalog(point, task)
    decisions = []
    for i, c in enumerate(draft['concepts'], 1):
        decisions.append(dict(candidate_id=f'C{i}', support_status='supported',
            evidence_ids=[catalog['sources'][0]['source_id']], source_reason='原文支持',
            role_correct=True, role_reason='分类合理', translation_consistent=None, translation_reason='',
            terms=[dict(term_id=f'C{i}.T{j}', substitutable=True, reason='等义') for j, _ in enumerate(c['terms'], 1)],
            aliases=[dict(alias_id=f'C{i}.A{j}', substitutable=True, reason='等义') for j, _ in enumerate(c.get('alias', []), 1)]))
    good = dict(decisions=decisions, missing=[])
    bad = deepcopy(good)
    bad['decisions'][0]['evidence_ids'] = ['S-other-task']
    patch = dict(decisions=[good['decisions'][0]], missing=[])
    client = AsyncStub(*(json.dumps(x, ensure_ascii=False) for x in [
        {'concepts': draft['concepts']}, bad, patch, {'strategies':draft['strategies']}]))
    agent = SearchPlannerAgent(client, concept_review=True, concept_review_protocol='v2')
    plan = asyncio.run(agent.aplan(point, task)) if async_mode else agent.plan(point, task)
    assert len(plan.strategies) == 3
    assert 'evidence.id' in client.calls[2][0][1].content
    assert 'source_quote' not in client.calls[1][0][1].content
    from novelty_agent_framework.core.concept_audit import read_attempt
    folder = next((tmp_path/'outputs/planner-attempts').iterdir())/'planner'
    index = json.loads((folder/'index.json').read_text())
    records = read_attempt(folder, run_id=index['run_id'], point_id=point.point_id, task_id=task.task_id)
    assert 'sources.json' in records
    assert 'concept_evidence_resolved' in records['events.jsonl'].decode()
    assert 'concept_contract_patch_merged' in records['events.jsonl'].decode()


def test_role_patch_changes_only_issued_role():
    from novelty_agent_framework.core.concept_repair_v2 import RoleRepair, apply_role_repair
    raw = {'concepts':[{'role':'operation','terms':['注意力门'],'alias':[]}]}
    errors = [{'loc':('concepts',0,'role')}]
    patch = RoleRepair.model_validate({'updates':[{'field_path':'concepts.0.role','role':'method'}]})
    fixed = apply_role_repair(raw, errors, patch)
    assert fixed['concepts'][0]['role'] == 'method'
    assert fixed['concepts'][0]['terms'] == raw['concepts'][0]['terms']
    assert raw['concepts'][0]['role'] == 'operation'
    patch.updates[0].field_path = 'concepts.1.role'
    with pytest.raises(ValueError, match='role.scope'):
        apply_role_repair(raw, errors, patch)


def test_scoped_correction_rejects_fake_news_identity_swap():
    from novelty_agent_framework.core.concept_stage import Correction
    from novelty_agent_framework.core.concept_repair_v2 import validate_scoped_correction
    original = dict(role='object',terms=['虚假新闻'],alias=[],exclude=[],importance=1)
    payload = dict(candidate=original, issues=[dict(code='role',allowed_fields=['role']),dict(code='source',allowed_fields=[])])
    bad = Correction.model_validate({'concept':{**original,'role':'method','terms':['新闻摘要','预训练模型','事实核验'],'importance':3}})
    with pytest.raises(ValueError, match='correction.scope'):
        validate_scoped_correction(bad, payload)
    good = Correction.model_validate({'concept':{**original,'role':'feature'}})
    validate_scoped_correction(good, payload)


def test_or_group_repair_cannot_introduce_another_mechanism():
    from novelty_agent_framework.core.concept_stage import Correction
    from novelty_agent_framework.core.concept_repair_v2 import validate_scoped_correction
    original = dict(role='method',terms=['新闻摘要','事实核验'],alias=[],exclude=[],importance=3)
    payload = dict(candidate=original,issues=[dict(code='terms',allowed_fields=['terms'])])
    validate_scoped_correction(Correction.model_validate({'concept':{**original,'terms':['新闻摘要']}}),payload)
    with pytest.raises(ValueError, match='correction.identity'):
        validate_scoped_correction(Correction.model_validate({'concept':{**original,'terms':['另一个机制']}}),payload)
