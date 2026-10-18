"""All three supplement needs must receive real Coordinator model planning."""
import asyncio
import json
import threading
from dataclasses import replace
from pathlib import Path

import pytest
from backend.env import ModelCallBudgetExceeded, ModelResponse, PromptLibrary
from novelty_agent_framework.agents import DemoCoordinator, NoveltyCoordinatorAgent
from novelty_agent_framework.schemas import NoveltyPointReview, SupplementRequest, TaskResearchResult
from novelty_agent_framework.schemas.failures import RecoveryAction as A, RecoveryDecision
from test_recovery_closure import failed_search
from test_workflow import (
    _final_evidence_sufficiency_state, build_workflow, make_paper,
    RecordingPlanner, RecordingTaskResearcher,
)

PROMPTS = Path('backend/src/novelty_agent_framework/prompts').resolve()


@pytest.fixture(autouse=True)
def isolate(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)


def task(point='NP-1', **updates):
    return dict(task_id='model-id', novelty_point_id=point, language='en',
                task_type='research_gap', description='查找机制比较证据，扩展原查询范围。', attempt=99,
                **updates)


class Client:
    def __init__(self, response=None, error=None):
        self.auto_response = response is None
        self.response = [task()] if response is None else response
        self.error = error
        self.calls = []
        self.threads = []
        self.action = "research_gap"

    def complete(self, messages, *, options=None):
        self.calls.append(messages)
        self.threads.append(threading.get_ident())
        if self.error:
            raise self.error
        if options.response_format.get('type') == 'json_schema':
            choices = options.response_format['json_schema']['schema']['properties']['option_id']['enum']
            return ModelResponse(content=json.dumps({'option_id': choices[-1]}))
        from novelty_agent_framework.core.recovery_planning import TRIGGER_TASK_TYPES
        marker = '当前独立触发码：'
        code = messages[1].content.split(marker, 1)[1].splitlines()[0].strip()
        response = self.response
        if self.auto_response:
            response = [{**task(), 'task_type': TRIGGER_TASK_TYPES[code]}]
        if isinstance(response, list):
            response = {"trigger_code": code, "decisions": [{"point_id": "NP-1", "action": "replan_query" if code == "coverage.no_match" else "research_gap",
                "reason": "Model selected a controlled comparison to address the remaining gap.",
                "research_tasks": response}] if response else []}
        return ModelResponse(content=json.dumps(response))


def setup(kind, *, client=None, max_rounds=2):
    client = client or Client()
    client.action = "replan_query" if kind == "zero" else "research_gap"
    workflow, _ = build_workflow(max_rounds=max_rounds)
    coordinator = NoveltyCoordinatorAgent(model_client=client, prompts=PromptLibrary(PROMPTS))
    workflow.services = replace(workflow.services, coordinator=coordinator)
    state = _final_evidence_sufficiency_state(0 if kind == 'count' else 1)
    state.update(run_id='run-supplement-test', novelty_points=state['brief'].novelty_points,
                 research_tasks=state['brief'].research_tasks,
                 all_research_tasks=state['brief'].research_tasks)
    executions = []
    if kind == 'semantic':
        state['novelty_reviews'] = [NoveltyPointReview(novelty_point_id='NP-1',
            status='insufficient_evidence', incomplete_reason='semantic_evidence',
            supplement_request=SupplementRequest(reason='Missing comparison', missing_aspects=['mechanism'],
                                                 suggested_focus=['Controlled comparison']))]
    if kind == 'zero':
        e = failed_search()
        executions = [type(e).model_validate({**e.model_dump(), 'status': 'succeeded', 'failure': None, 'error': None})]
    state['task_research_results'] = [TaskResearchResult(task_id='T-1', novelty_point_id='NP-1',
        status='completed', steps_used=1, search_executions=executions)]
    return workflow, state, client


@pytest.mark.parametrize('kind,code', [('count','evidence.missing'),
    ('semantic','review.missing_features'), ('zero','coverage.no_match')])
def test_three_independent_needs_reach_llm_and_bind_planned_decisions(kind, code):
    workflow, state, client = setup(kind)
    checked = asyncio.run(workflow._check_final_evidence_sufficiency(state))
    assert code in checked['recovery_decisions'][0].trigger_codes
    if kind != 'count':
        assert checked['insufficient_final_evidence_points'] == []
    combined = {**state, **checked}
    assert asyncio.run(workflow._route_after_evidence_sufficiency_check(combined)) == 'supplement'
    planned = asyncio.run(workflow._plan_supplement(combined))
    assert len(client.calls) == 2
    assert client.threads[0] != threading.get_ident()
    prompt = client.calls[0][1].content
    assert code in prompt and 'T-1' in prompt
    assert state['paper'].full_text not in prompt
    if kind == 'semantic':
        assert 'mechanism' in prompt
        assert 'Missing comparison' in prompt and 'Controlled comparison' in prompt
        assert 'feature_catalog' in prompt
    if kind == 'zero':
        assert 'original query' in prompt
        assert 'original query' in planned['research_tasks'][0].description
    decision = planned['recovery_decisions'][0]
    assert decision.coordinator_status == 'planned'
    assert decision.planning_origin == 'coordinator_llm'
    assert decision.reason.startswith('Model selected')
    assert decision.coordinator_guidance_ids
    task_id = "T-R2-" + {"count":"COUNT", "semantic":"SEMANTIC", "zero":"QUERY"}[kind] + "-1"
    assert decision.planned_task_ids == [task_id]
    assert planned['research_tasks'][0].attempt == 2
    assert planned['recovery_search_plans'] == {}
    assert planned['recovery_directives'][json.dumps(['NP-1', task_id])] == decision
    audit = Path('outputs/paper-test/recovery/run-supplement-test/round-1-coordinator.json')
    audit_data = json.loads(audit.read_text())
    assert audit_data['decisions'][0]['coordinator_status'] == 'planned'
    assert audit_data['tasks'][0]['task_id'] == task_id


def test_successful_empty_and_failed_query_trigger_together_and_keep_failures():
    workflow, state, client = setup('zero')
    failed = failed_search(503).model_copy(update={'execution_id':'failed-query'})
    state['task_research_results'][0].search_executions.append(failed)
    checked = asyncio.run(workflow._check_final_evidence_sufficiency(state))
    assert checked['recovery_decisions'][0].action == A.REPLAN_QUERY
    planned = asyncio.run(workflow._plan_supplement({**state, **checked}))
    assert planned['research_tasks']
    assert 'provider.service' in client.calls[0][1].content
    assert failed.failure.event_id in checked['recovery_decisions'][0].cause_event_ids


@pytest.mark.parametrize('status', ['partial', 'failed', 'not_run'])
def test_without_successful_query_no_zero_candidate_trigger(status):
    workflow, state, client = setup('zero')
    execution = state['task_research_results'][0].search_executions[0]
    state['task_research_results'][0].search_executions = [type(execution).model_validate(
        {**execution.model_dump(), 'status': status})]
    checked = asyncio.run(workflow._check_final_evidence_sufficiency(state))
    assert checked['recovery_decisions'] == []
    assert client.calls == []


def test_historical_empty_queries_do_not_trigger_new_round():
    workflow, state, _ = setup('zero', max_rounds=3)
    state['rounds'] = 2
    state['research_tasks'] = [t.model_copy(update={'task_id': 'T-R2-1', 'attempt':2})
                               for t in state['research_tasks'][:1]]
    checked = asyncio.run(workflow._check_final_evidence_sufficiency(state))
    assert checked['recovery_decisions'] == []


def test_one_empty_success_does_not_trigger_when_another_query_has_candidates():
    from novelty_agent_framework.schemas.references import SearchExecution, SearchResultRef
    workflow, state, _ = setup('zero')
    successful = state['task_research_results'][0].search_executions[0]
    partial = SearchExecution.model_validate({**successful.model_dump(), 'execution_id':'partial-hit',
        'status':'partial', 'results':[SearchResultRef(source_record_id='candidate-1', rank=1)]})
    state['task_research_results'][0].search_executions.append(partial)
    checked = asyncio.run(workflow._check_final_evidence_sufficiency(state))
    assert checked['recovery_decisions'] == []


def test_another_runs_search_cannot_trigger_zero_candidates():
    workflow, state, _ = setup('zero')
    state['task_research_results'][0].search_executions[0].run_id = 'other-run'
    checked = asyncio.run(workflow._check_final_evidence_sufficiency(state))
    assert checked['recovery_decisions'] == []


def test_multiple_trigger_codes_have_independent_decisions_calls_and_tasks():
    workflow, state, client = setup('zero')
    state['evidence_cards'] = []
    state['novelty_reviews'] = [NoveltyPointReview(novelty_point_id='NP-1',
        status='insufficient_evidence', incomplete_reason='semantic_evidence')]
    checked = asyncio.run(workflow._check_final_evidence_sufficiency(state))
    assert len(checked['recovery_decisions']) == 3
    assert {d.trigger_codes[0] for d in checked['recovery_decisions']} == {
        'evidence.missing', 'review.missing_features', 'coverage.no_match'}
    planned = asyncio.run(workflow._plan_supplement({**state, **checked}))
    assert len(client.calls) == 6
    assert len(planned['research_tasks']) == 3
    assert {t.task_type for t in planned['research_tasks']} == {'research_gap', 'feature_supplement', 'replan_query'}
    assert len({t.task_id for t in planned['research_tasks']}) == 3


@pytest.mark.parametrize('response', [[], [task('NP-99')],
    [{**task(), 'task_type':'read_artifact'}], [{**task(), 'language':'zh'}],
    [task(), task()], [{'invalid':'schema'}]])
def test_invalid_planning_stops_without_rule_fallback_or_spending_round(response):
    workflow, state, client = setup('count', client=Client(response=response))
    checked = asyncio.run(workflow._check_final_evidence_sufficiency(state))
    planned = asyncio.run(workflow._plan_supplement({**state, **checked}))
    assert len(client.calls) == 2
    assert planned['research_tasks'] == []
    assert 'rounds' not in planned
    assert planned['recovery_decisions'][0].coordinator_status == 'failed'
    assert planned['recovery_decisions'][0].action == A.STOP
    assert planned['issues']
    assert asyncio.run(workflow._route_after_supplement_plan({**state, **planned})) == 'synthesize_report'


@pytest.mark.parametrize('error', [ModelCallBudgetExceeded('budget'), TimeoutError('timeout')])
def test_model_execution_failure_is_recorded_and_stops(error):
    workflow, state, _ = setup('count', client=Client(error=error))
    checked = asyncio.run(workflow._check_final_evidence_sufficiency(state))
    planned = asyncio.run(workflow._plan_supplement({**state, **checked}))
    assert planned['research_tasks'] == []
    failure = planned['failure_events'][-1]
    assert failure.scope.node == 'coordinator.plan_supplement'
    assert failure.event_id in planned['recovery_decisions'][0].cause_event_ids


def test_round_limit_never_calls_coordinator():
    workflow, state, client = setup('count', max_rounds=1)
    checked = asyncio.run(workflow._check_final_evidence_sufficiency(state))
    result = asyncio.run(workflow._plan_supplement({**state, **checked}))
    assert result['research_tasks'] == []
    assert client.calls == []


def test_model_cannot_plan_tasks_for_stopped_point_or_silently_omit_a_point():
    from novelty_agent_framework.core.recovery_planning import validate_supplement_tasks
    workflow, state, _ = setup('count')
    second = state['brief'].novelty_points[0].model_copy(update={'point_id':'NP-2'})
    brief = state['brief'].model_copy(update={'novelty_points':[*state['brief'].novelty_points, second]})
    active = RecoveryDecision(point_id='NP-1', action=A.RESEARCH_GAP, reason='count')
    stopped = RecoveryDecision(point_id='NP-2', action=A.STOP, reason='budget', max_additional_attempts=0)
    with pytest.raises(ValueError, match='未获准'):
        validate_supplement_tasks([task(), task('NP-2')], brief=brief, decisions=[active, stopped],
                                 languages=['en'], attempt=2)
    second_active = RecoveryDecision(point_id='NP-2', action=A.RESEARCH_GAP, reason='semantic')
    with pytest.raises(ValueError, match='全部补检决策'):
        validate_supplement_tasks([task()], brief=brief, decisions=[active, second_active],
                                 languages=['en'], attempt=2)


class PlanningModelCoordinator(NoveltyCoordinatorAgent):
    # Real model planning path; deterministic synthesis keeps this test offline.
    def synthesize(self, *args, **kwargs):
        return DemoCoordinator().synthesize(*args, **kwargs)


@pytest.mark.parametrize('response,expected_rounds', [([task()],2), ([],1)])
def test_complete_graph_preserves_old_cards_and_records_planning_outcome(response, expected_rounds):
    class IncompleteReviewer:
        async def review(self, request):
            return NoveltyPointReview(novelty_point_id=request.novelty_point.point_id,
                status='insufficient_evidence', incomplete_reason='material_unavailable')
    planner = RecordingPlanner()
    workflow, researcher = build_workflow(RecordingTaskResearcher(), planner=planner,
        min_final_evidence_cards_per_point=2, max_rounds=2)
    client = Client(response=response)
    workflow.services = replace(workflow.services, reviewer=IncompleteReviewer(),
        coordinator=PlanningModelCoordinator(model_client=client, prompts=PromptLibrary(PROMPTS)))
    result = workflow.run(make_paper())
    assert result.rounds == expected_rounds
    assert len(client.calls) == 2
    assert len(planner.calls) == expected_rounds
    assert len(researcher.calls) == expected_rounds
    assert len(result.evidence_cards) == expected_rounds
    assert any(c.task_id == 'T-1' for c in result.evidence_cards)
    recorded = [d for d in result.recovery_decisions if d.coordinator_status != 'not_requested']
    assert len(recorded) == 1
    assert recorded[0].coordinator_status == ('planned' if response else 'failed')
    assert result.report.recovery_decisions == result.recovery_decisions


def test_decision_contract_does_not_allow_planning_receipt_on_stop():
    with pytest.raises(ValueError, match='task-level'):
        RecoveryDecision(point_id='NP-1', action=A.STOP, reason='limit', max_additional_attempts=0,
            coordinator_status='planned', planned_task_ids=['T-R2-1'])
