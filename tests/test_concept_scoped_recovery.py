import json

import pytest

from backend.env import ModelResponse
from test_search_planner import SearchPlannerAgent, make_point, make_task, valid_draft


@pytest.mark.parametrize('mode', ['role', 'alias', 'scope', 'invalid', 'unchanged'])
def test_full_v2_scoped_recovery_and_alias_isolation(tmp_path, monkeypatch, mode):
    monkeypatch.chdir(tmp_path)
    point, task = make_point(), make_task()
    draft = valid_draft(task)
    if mode == 'alias':
        draft['concepts'][0]['alias'] = ['不等义别名']

    class Client:
        corrections = 0
        def complete(self, messages, *, options=None):
            payload, _ = json.JSONDecoder().raw_decode(messages[1].content)
            if 'needs_translation' in payload:
                value = json.loads(json.dumps({'concepts':draft['concepts']}))
                if mode == 'role':
                    value['concepts'][0]['role'] = 'operation'
            elif 'invalid_roles' in payload:
                value = {'updates':[{'field_path':k,'role':draft['concepts'][0]['role']} for k in payload['invalid_roles']]}
            elif 'candidates' in payload:
                ds = []
                for cid,c in payload['candidates'].items():
                    ds.append(dict(candidate_id=cid, support_status='supported', evidence_ids=[payload['source_catalog']['sources'][0]['source_id']],
                        source_reason='输入明确支持', role_correct=not(mode in ('scope','invalid','unchanged') and payload['check_coverage'] and cid=='C1'),
                        role_reason='待修正分类', translation_consistent=None, translation_reason='',
                        terms=[dict(term_id=k,substitutable=True,reason='等义') for k in c['terms']],
                        aliases=[dict(alias_id=k,substitutable=False,reason='不是同义表达') for k in c['aliases']],
                        issue_resolution=('invalid' if mode=='invalid' else 'fixed') if not payload['check_coverage'] else None,
                        resolution_reason='分类字段已修正' if not payload['check_coverage'] else '',
                        issue_ids=[i['issue_id'] for i in payload.get('issues',[])]))
                value = dict(decisions=ds,missing=[])
            elif 'candidate' in payload:
                self.corrections += 1
                value = {'concept':dict(payload['candidate']) if mode in ('invalid','unchanged') else
                    {**payload['candidate'],'role':'feature' if payload['candidate']['role']!='feature' else 'method'}}
                if mode=='scope' and self.corrections == 1:
                    value['concept']['terms'] = ['被偷换的另一机制']
            else:
                value = {'strategies':draft['strategies']}
            return ModelResponse(content=json.dumps(value,ensure_ascii=False))

    client = Client()
    plan = SearchPlannerAgent(client,concept_review=True,concept_review_protocol='v2').plan(point,task)
    assert plan.strategies
    root = next((tmp_path/'outputs/planner-attempts').iterdir())/'planner'
    events = [json.loads(x) for x in (root/'events.jsonl').read_text().splitlines()]
    if mode == 'role':
        assert any(e['step']=='concept_roles_patched' for e in events)
    elif mode == 'alias':
        assert not plan.concepts[0].alias
        assert any(e['step']=='concept_alias_quarantined' for e in events)
        assert not client.corrections
    elif mode=='scope':
        assert client.corrections == 2
        assert plan.concepts[0].terms == draft['concepts'][0]['terms']
        solved = next(e['payload'] for e in events if e['step']=='concept_issue_resolved')
        assert solved['issue_ids'] == ['I-C1-role']
    else:
        summary = json.loads((root/'summary.json').read_text())
        if mode=='invalid':
            assert client.corrections==1
            assert summary['metrics']['semantic_invalid']==1
            assert summary['metrics']['content_changes']==0
        else:
            assert client.corrections==3
            assert summary['status']=='partial'
            assert summary['metrics']['semantic_unresolved']==3
            assert all('C1-role' in key for d in json.loads((root/'issues.json').read_text())['current'].values() for key in [d[0]['issue_id']])
