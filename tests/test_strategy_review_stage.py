import asyncio
import json

import pytest

from backend.env import ModelResponse
from backend.env.model_client import ModelCallBudgetExceeded
from test_search_planner import SearchPlannerAgent, make_point, make_task, valid_draft
from novelty_agent_framework.schemas.search_plan_draft import SearchConceptDraft
from novelty_agent_framework.core.strategy_stage import StrategyChoice, StrategyVerdict, run_strategy_stage


def verdict(**changes):
    return StrategyVerdict(decision='pass', concept_use_correct=True, mechanism_preserved=True,
        level_appropriate=True, adjustment_reasonable=True, reason='机制完整且档位调整合理', **changes)


def drafts():
    return [SearchConceptDraft.model_validate(c) for c in valid_draft(make_task())['concepts']]


def test_order_independent_review_and_partial_levels():
    seen, events = [], []
    async def call(op, schema, payload):
        seen.append((op, payload))
        if op == 'generate':
            return StrategyChoice(expression={'strict':'C1 AND (C2 OR C3)', 'medium':'C1 AND C2', 'broad':'C1'}[payload['level']])
        if payload['level'] == 'broad':
            return verdict().model_copy(update={'decision':'revise', 'mechanism_preserved':False,
                'reason':'只剩泛称', 'missing_mechanisms':['动态图训练机制']})
        return verdict()
    plan = asyncio.run(run_strategy_stage(make_point(), make_task(), drafts(), call, lambda a,s,p:events.append((s,p))))
    assert [s.level for s in plan.strategies] == ['strict','medium']
    broad = [p for op,p in seen if op=='generate' and p['level']=='broad']
    assert len(broad) == 4  # first try plus three semantic repairs
    assert all(p['previous_passed']['strategy']['level']=='medium' for p in broad)
    assert 'expanded_query' in next(p for op,p in seen if op=='review')
    assert all('feedback' not in p and 'previous_candidate' not in p for op,p in seen if op=='review')
    medium_review = next(p for op,p in seen if op=='review' and p['level']=='medium')
    assert medium_review['comparison']['identical_expanded_query'] is False
    assert medium_review['comparison']['removed_concept_ids'] == ['C3']
    assert 'broad' in events[-1][1]['failed_levels']


def test_duplicate_actual_query_is_not_a_new_level():
    reviews = []
    async def call(op, schema, payload):
        if op == 'generate':
            return StrategyChoice(expression='C1 AND C2')
        reviews.append(payload['level'])
        return verdict()
    plan = asyncio.run(run_strategy_stage(make_point(), make_task(), drafts(), call, lambda *args:None))
    assert [s.level for s in plan.strategies] == ['strict']
    assert reviews == ['strict']


def test_failed_strict_does_not_become_previous_passed():
    previous = []
    async def call(op, schema, payload):
        if op=='generate':
            previous.append((payload['level'],payload['previous_passed']))
            return StrategyChoice(expression={'strict':'C1 AND C2','medium':'C1','broad':'C2'}[payload['level']])
        if payload['level']=='strict':
            return verdict().model_copy(update={'decision':'uncertain'})
        return verdict()
    plan = asyncio.run(run_strategy_stage(make_point(), make_task(), drafts(), call, lambda *args:None))
    assert plan.strategies[0].level == 'medium'
    assert next(p for level,p in previous if level=='medium') is None


@pytest.mark.parametrize('failure', [ModelCallBudgetExceeded('budget'), asyncio.CancelledError()])
def test_budget_or_cancel_preserves_progress_and_stops_calls(failure):
    events, levels = [], []
    async def call(op, schema, payload):
        levels.append(payload['level'])
        if payload['level']=='medium':
            raise failure
        return StrategyChoice(expression='C1 AND C2') if op=='generate' else verdict()
    with pytest.raises(type(failure)):
        asyncio.run(run_strategy_stage(make_point(), make_task(), drafts(), call, lambda a,s,p:events.append((s,p))))
    assert 'broad' not in levels
    progress = [p for s,p in events if s=='strategy_review_progress'][-1]
    assert progress['accepted_strategies'][0]['level'] == 'strict'


@pytest.mark.parametrize('field', ['concept_use_correct','mechanism_preserved','level_appropriate','adjustment_reasonable'])
def test_pass_word_cannot_override_a_failed_semantic_dimension(field):
    assert not verdict().model_copy(update={field:False}).accepted


@pytest.mark.parametrize('async_mode', [False,True])
def test_full_planner_strategy_contract_repair_and_audit(tmp_path, monkeypatch, async_mode):
    from novelty_agent_framework.core.concept_audit import read_attempt
    monkeypatch.chdir(tmp_path)
    class Client:
        generated = 0
        def complete(self, messages, *, options=None):
            payload,_ = json.JSONDecoder().raw_decode(messages[1].content)
            if 'current_strategy' in payload:
                out = verdict().model_dump()
            else:
                self.generated += 1
                out = dict(expression='C999' if self.generated==1 else
                    {'strict':'C1 AND (C2 OR C3)','medium':'C1 AND C2','broad':'C1'}[payload['level']])
            return ModelResponse(content=json.dumps(out,ensure_ascii=False))
        async def acomplete(self, messages, *, options=None):
            return self.complete(messages,options=options)
    agent = SearchPlannerAgent(Client(),concept_review=True,concept_review_protocol='v2',strategy_review=True)
    async def fixed_concepts(point, task, use_async, record):
        record(0,'concept_stage_finished',{'accepted':[1,2,3,4,5,6],'unresolved':{}})
        return [c.model_dump() for c in drafts()]
    agent._concept_phase = fixed_concepts
    point, task = make_point(), make_task()
    plan = asyncio.run(agent.aplan(point,task)) if async_mode else agent.plan(point,task)
    assert len(plan.strategies)==3
    folder = next((tmp_path/'outputs/planner-attempts').iterdir())/'planner'
    idx=json.loads((folder/'index.json').read_text())
    records=read_attempt(folder,run_id=idx['run_id'],point_id=point.point_id,task_id=task.task_id)
    summary=json.loads(records['summary.json'])
    assert summary['strategy_calls']==7
    assert summary['metrics']['strategy_contract_retry']==1
    assert summary['status']=='completed'
    assert b'C999' in records['strategy-calls/call-00001.json']


def test_unreviewed_concepts_cannot_enable_new_strategy_path():
    with pytest.raises(ValueError,match='frozen reviewed'):
        SearchPlannerAgent(strategy_review=True)


def test_reviewed_strategy_config_reaches_both_factories():
    from novelty_agent_framework.config import load_application_config, build_model_registry, build_workflow
    from novelty_agent_framework.config.loader import legacy_shape
    from novelty_agent_framework.config.factory import build_search_planner, build_prompt_library
    config=load_application_config(environ={},overrides={'search_planner':{
        'concept_review':True,'concept_review_protocol':'v2','strategy_review':True}})
    legacy=build_search_planner(legacy_shape(config),build_model_registry(config),build_prompt_library(),
        retrieval_cfg={'active_source':'springer'})
    typed=build_workflow(config).services.search_planner
    assert legacy.strategy_review and typed.strategy_review
    assert legacy.strategy_database=='springer'
    assert typed.strategy_database==config.researcher.tools.database_search.active_source
