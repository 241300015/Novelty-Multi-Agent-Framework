import asyncio
from copy import deepcopy
import json

import pytest

from backend.env import ModelResponse
from test_search_planner import make_point, make_task, SearchPlannerAgent
from novelty_agent_framework.core.task_semantics import (
    SemanticFacts, SemanticPolicy, build_task_contract, missing_requirements,
    validate_contract_binding, validate_facts, validate_policy,
)
from novelty_agent_framework.core.concept_evidence import build_catalog
from novelty_agent_framework.core.strategy_stage import StrategyChoice, StrategyVerdict, run_strategy_stage
from novelty_agent_framework.schemas.search_plan_draft import SearchConceptDraft


def fixture():
    point=make_point().model_copy(update={'claim':'去除特征变换和非线性激活，保留邻域聚合。',
        'claim_en':'','technical_features':['对各层嵌入加权求和。'],'technical_features_en':[]})
    task=make_task()
    return point,task


def fact_response(catalog):
    facts=[]
    for action,obj in [('removes','特征变换'),('removes','非线性激活'),('retains','邻域聚合'),('uses','嵌入加权求和')]:
        facts.append(dict(subject='图模型',action=action,object=obj,statement=action+':'+obj,
            importance='defining' if len(facts)<3 else 'supporting',
            evidence_ids=[catalog['sources'][0 if len(facts)<3 else -1]['source_id']]))
    return dict(research_goal='简化图传播网络',facts=facts)


def policy_response():
    def level(groups):
        return dict(requirements=[dict(any_of=g,reason='保留辨识机制') for g in groups],relaxation_reason='支持细节可省略')
    return dict(strict=level([['F1'],['F2'],['F3']]),
        medium=level([['F1','F2'],['F3']]),broad=level([['F1','F2'],['F3']]))


def contract_fixture():
    point,task=fixture()
    seen=[]
    async def call(op,schema,payload):
        assert 'candidates' not in payload and 'current_strategy' not in payload
        seen.append(op)
        return schema.model_validate(fact_response(payload['source_catalog']) if op=='facts' else policy_response())
    contract=asyncio.run(build_task_contract(point,task,call,lambda *a:None))
    assert seen==['facts','policy']
    return point,task,contract


def test_source_first_contract_binding_and_tampering():
    point,task,contract=contract_fixture()
    validate_contract_binding(contract,point,task)
    with pytest.raises(ValueError,match='semantics.binding'):
        validate_contract_binding(contract,point,task.model_copy(update={'task_id':'another-task'}))
    changed=deepcopy(contract)
    changed['levels']['broad']['requirements']=[]
    with pytest.raises(ValueError,match='semantics.integrity'):
        validate_contract_binding(changed,point,task)


def test_fabricated_evidence_and_context_only_policy_rejected():
    point,task=fixture()
    catalog=build_catalog(point,task)
    response=SemanticFacts.model_validate(fact_response(catalog))
    response.facts[0].evidence_ids=['invented']
    with pytest.raises(ValueError,match='evidence.id'):
        validate_facts(response,point,task,catalog)
    _,_,contract=contract_fixture()
    policy=SemanticPolicy.model_validate(policy_response())
    policy.broad.requirements[0].any_of=['F4']
    policy.broad.requirements[1].any_of=['F4']
    with pytest.raises(ValueError,match='discriminative'):
        validate_policy(policy,contract['facts'])


@pytest.mark.parametrize('expression,expected',[
    ('C1 AND C3',False), # optional weighted embedding need not be ANDed
    ('(C1 OR C2) AND C3',False),
    ('C1 OR C3',True), # each branch misses a required mechanism group
    ('(C1 AND C3) OR C4',True), # one good branch cannot hide a weak branch
    ('C1 AND (C3 OR C4)',True),
])
def test_every_or_branch_obeys_frozen_minimum(expression,expected):
    _,_,contract=contract_fixture()
    links={'C1':['F1'],'C2':['F2'],'C3':['F3'],'C4':['F4']}
    assert bool(missing_requirements(expression,links,contract,'broad')) is expected


def test_strategy_precheck_rejects_weak_or_branch_without_model_approval():
    point,task,contract=contract_fixture()
    drafts=[SearchConceptDraft(terms=[f['object']],role='method') for f in contract['facts'].values()]
    reviewed=[]
    events=[]
    async def call(op,schema,payload):
        assert payload['semantic_contract']['contract_id']==contract['contract_id']
        if op=='generate':
            return StrategyChoice(expression={'strict':'C1 AND C2 AND C3','medium':'C1 AND C3','broad':'C1 OR C3'}[payload['level']])
        reviewed.append(payload['level'])
        return StrategyVerdict(decision='pass',concept_use_correct=True,mechanism_preserved=True,
            level_appropriate=True,adjustment_reasonable=True,reason='通过冻结判据')
    plan=asyncio.run(run_strategy_stage(point,task,drafts,call,lambda a,s,p:events.append(s),
        semantic_contract=contract,concept_fact_links={f'C{i}':[f'F{i}'] for i in range(1,5)}))
    assert [s.level for s in plan.strategies]==['strict','medium']
    assert reviewed==['strict','medium']
    assert events.count('strategy_fact_rejected')==4


def test_semantic_contract_runs_before_candidates_and_reaches_all_reviews(tmp_path,monkeypatch):
    monkeypatch.chdir(tmp_path)
    point,task=fixture()
    operations=[]
    class Client:
        def complete(self,messages,*,options=None):
            payload,_=json.JSONDecoder().raw_decode(messages[1].content)
            if 'facts' in payload and 'candidates' not in payload and 'level' not in payload:
                operations.append('policy'); out=policy_response(); out['$defs']={'ignored_schema_metadata':{}}
            elif 'source_catalog' in payload and 'semantic_contract' not in payload:
                operations.append('facts'); out=fact_response(payload['source_catalog'])
            elif 'needs_translation' in payload:
                operations.append('concepts')
                assert 'semantic_contract' in payload
                out={'concepts':[dict(role='method',terms=[f['object']]) for f in payload['semantic_contract']['facts'].values()]}
            elif 'candidates' in payload:
                operations.append('concept-review')
                out=dict(decisions=[dict(candidate_id=cid,support_status='supported',evidence_ids=[payload['source_catalog']['sources'][0]['source_id']],
                    fact_ids=[cid.replace('C','F')],source_reason='事实支撑',role_correct=True,role_reason='分类合理',
                    translation_consistent=None,translation_reason='',aliases=[],
                    terms=[dict(term_id=tid,substitutable=True,reason='单词项') for tid in c['terms']]) for cid,c in payload['candidates'].items()],missing=[])
            elif 'current_strategy' in payload:
                operations.append('strategy-review')
                assert 'level_requirements' in payload
                out=dict(decision='pass',concept_use_correct=True,level_appropriate=True,adjustment_reasonable=True,reason='符合固定判据')
            else:
                operations.append('strategy')
                out=dict(expression={'strict':'C1 AND C2 AND C3','medium':'C1 AND C3','broad':'(C1 OR C2) AND C3'}[payload['level']])
            return ModelResponse(content=json.dumps(out,ensure_ascii=False))
    agent=SearchPlannerAgent(Client(),concept_review=True,concept_review_protocol='v2',strategy_review=True,semantic_contract=True)
    plan=agent.plan(point,task)
    assert len(plan.strategies)==3
    assert operations[:4]==['facts','policy','concepts','concept-review']
    root=next((tmp_path/'outputs/planner-attempts').iterdir())/'planner'
    assert (root/'semantic-contract.json').exists() and (root/'concept-fact-links.json').exists()
    summary=json.loads((root/'summary.json').read_text())
    assert summary['semantic_calls']==2
    assert summary['strategy_calls']==6


def test_optional_supporting_detail_cannot_become_mandatory_in_relaxed_policy():
    _,_,contract=contract_fixture()
    policy=SemanticPolicy.model_validate(policy_response())
    from novelty_agent_framework.core.task_semantics import FactRequirement
    policy.broad.requirements.append(FactRequirement(any_of=['F4'],reason='错误地强制实现细节'))
    with pytest.raises(ValueError,match='semantics.relaxation'):
        validate_policy(policy,contract['facts'])


def test_review_cannot_move_frozen_goalposts_but_may_reject_wrong_mapping():
    from novelty_agent_framework.core.task_semantics import validate_strategy_judgment
    _,_,contract=contract_fixture()
    payload=dict(semantic_contract=contract,required_groups_satisfied=True,concepts=[{'concept_id':'C1'}])
    verdict=StrategyVerdict(decision='revise',concept_use_correct=True,mechanism_preserved=False,
        level_appropriate=True,adjustment_reasonable=True,reason='坚持要求可选细节',missing_mechanisms=['嵌入加权求和'])
    with pytest.raises(ValueError,match='review.criteria'):
        validate_strategy_judgment(verdict,payload)
    verdict.fact_mapping_errors={'C1':'使用非线性激活不等于去除非线性激活'}
    validate_strategy_judgment(verdict,payload)
    assert not verdict.accepted


def test_combined_fake_news_steps_cannot_bypass_fact_checks_as_one_or_group():
    from novelty_agent_framework.core.task_semantics import validate_main_terms
    with pytest.raises(ValueError,match='semantics.canonical'):
        validate_main_terms([SearchConceptDraft(terms=['新闻摘要','预训练模型','事实核验'],role='method')])
    validate_main_terms([SearchConceptDraft(terms=['新闻摘要'],role='method'),
                         SearchConceptDraft(terms=['事实核验'],role='method')])


def test_model_sees_source_ids_not_competing_integrity_hashes():
    from novelty_agent_framework.core.task_semantics import model_source_view
    point,task=fixture()
    catalog=build_catalog(point,task)
    view=model_source_view({'source_catalog':catalog})
    assert all(set(source)=={'source_id','text','field','language'} for source in view['source_catalog']['sources'])
    assert 'sha256' in catalog['sources'][0]


def test_invalid_automatic_standard_can_stop_without_forcing_a_pass():
    from novelty_agent_framework.core.strategy_stage import GroundedStrategyVerdict, SemanticContractRejected
    from novelty_agent_framework.core.task_semantics import validate_strategy_judgment
    point,task,contract=contract_fixture()
    drafts=[SearchConceptDraft(terms=[f['object']],role='method') for f in contract['facts'].values()]
    async def call(op,schema,payload):
        if op=='generate':
            return StrategyChoice(expression='C1 AND C2 AND C3')
        result=GroundedStrategyVerdict(decision='revise',concept_use_correct=True,level_appropriate=True,
            adjustment_reasonable=True,reason='判据没有保留原始任务的重要限制',contract_errors={'source_gap':'需要从原始输入重新检查限定条件'})
        validate_strategy_judgment(result.as_verdict(),payload)
        return result
    with pytest.raises(SemanticContractRejected):
        asyncio.run(run_strategy_stage(point,task,drafts,call,lambda *a:None,
            semantic_contract=contract,concept_fact_links={f'C{i}':[f'F{i}'] for i in range(1,5)}))
