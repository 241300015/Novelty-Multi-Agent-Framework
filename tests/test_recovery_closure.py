"""Recovery must follow observed gaps, not only final card counts."""
import asyncio
from datetime import datetime, timezone

import httpx

from novelty_agent_framework.core.failure_classification import provider_failure
from novelty_agent_framework.schemas.failures import FailureScope
from novelty_agent_framework.schemas import NoveltyPointReview, TaskResearchResult, SupplementRequest
from novelty_agent_framework.schemas.references import SearchExecution
from test_workflow import build_workflow, _final_evidence_sufficiency_state


def failed_search(status=406):
    response = httpx.Response(status, request=httpx.Request('GET', 'https://example.test/query'))
    exc = httpx.HTTPStatusError('HTTP failure', request=response.request, response=response)
    return SearchExecution(execution_id='search-1', tool_name='database_search',source_id='arxiv',query='original query',
        status='failed',started_at=datetime.now(timezone.utc),error='provider failed',
        failure=provider_failure(exc,scope=FailureScope(point_id='NP-1',provider='arxiv'),occurrence_id='search-1'))


def test_protocol_failure_does_not_repeat_same_search_without_configured_alternative():
    workflow, _ = build_workflow(max_rounds=2)
    state = _final_evidence_sufficiency_state(0)
    state['task_research_results'] = [TaskResearchResult(task_id='T-1',novelty_point_id='NP-1',status='completed',steps_used=2,search_executions=[failed_search()])]
    checked=asyncio.run(workflow._check_final_evidence_sufficiency(state))
    assert asyncio.run(workflow._route_after_evidence_sufficiency_check({**state,**checked}))=='synthesize'
    assert checked['recovery_decisions'][0].action.value=='stop'
    assert checked['recovery_decisions'][0].cause_event_ids


def test_semantic_feature_gap_can_supplement_even_when_card_count_passes():
    workflow, _ = build_workflow(max_rounds=2)
    state=_final_evidence_sufficiency_state(1)
    state['novelty_reviews']=[NoveltyPointReview(novelty_point_id='NP-1',status='insufficient_evidence',incomplete_reason='semantic_evidence',supplement_request=SupplementRequest(reason='missing method description',missing_aspects=['mechanism']))]
    checked=asyncio.run(workflow._check_final_evidence_sufficiency(state))
    assert checked['insufficient_final_evidence_points']==[]
    assert asyncio.run(workflow._route_after_evidence_sufficiency_check({**state,**checked}))=='supplement'
    assert checked['recovery_decisions'][0].missing_aspects==['mechanism']

import pytest
from pydantic import ValidationError
from novelty_agent_framework.core.recovery_policy import plan_recovery
from novelty_agent_framework.core.runtime_artifacts import _stage_debug_details
from novelty_agent_framework.schemas.failures import (FailureCode as C, RecoveryAction as A,
    RecoveryDecision, RecoveryArtifact, make_failure, FailureEvent)
from novelty_agent_framework.schemas.research import CandidateAuditRecord
from novelty_agent_framework.schemas.research_tools import DatabaseSearchArguments, ReaderCallArguments
from novelty_agent_framework.tools.recovery_registry import RecoveryToolRegistry
from novelty_agent_framework.tools import ResearcherToolRegistry
from novelty_agent_framework.schemas import ResearcherToolObservation
from test_task_researcher_workflow import scope, FakeBuilder, FakeModel, finish
from novelty_agent_framework.workflows import TaskResearcherWorkflow


@pytest.fixture(autouse=True)
def isolate_recovery_files(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)


@pytest.mark.parametrize('status,code',[(401,C.PROVIDER_AUTHENTICATION),(403,C.PROVIDER_AUTHORIZATION),
    (404,C.PROVIDER_RESOURCE_MISSING),(406,C.PROVIDER_PROTOCOL),(429,C.PROVIDER_RATE_LIMIT),(503,C.PROVIDER_SERVICE)])
def test_observed_provider_failure_codes_do_not_claim_a_root_cause_or_novelty(status,code):
    failure=failed_search(status).failure
    assert failure.code == code
    assert failure.semantic_status == 'not_adjudicated'
    assert 'underlying cause is not established' in failure.message
    assert 'example.test' not in failure.model_dump_json()


def test_failure_contract_rejects_invented_retry_permissions():
    event=make_failure(C.PROVIDER_PROTOCOL,scope=FailureScope(provider='arxiv'))
    changed=event.model_dump()
    changed['retry']={'allowed':True,'max_additional_attempts':2}
    with pytest.raises(ValidationError): FailureEvent.model_validate(changed)
    changed=event.model_dump();changed['category']='authorization'
    with pytest.raises(ValidationError): FailureEvent.model_validate(changed)


def policy(rows, *, reviews=(), history=(), provider_order=(), round_number=1):
    state=_final_evidence_sufficiency_state(0)
    from novelty_agent_framework.schemas import InsufficientFinalEvidence
    return plan_recovery(paper_id=state['paper'].paper_id,run_id='run-1',points=state['brief'].novelty_points,
        results=rows,reviews=reviews,insufficient=[InsufficientFinalEvidence(novelty_point_id='NP-1',valid_card_count=0,required_card_count=1)],
        round_number=round_number,max_rounds=3,provider_order=provider_order,history=history)


def task_result(**kwargs):
    return TaskResearchResult(task_id='T-1',novelty_point_id='NP-1',status='partial',steps_used=1,**kwargs)


def test_protocol_failure_leaves_provider_switching_to_local_correction():
    decisions,_=policy([task_result(search_executions=[failed_search()])],provider_order=('arxiv','openalex'))
    assert decisions[0].action==A.STOP
    assert decisions[0].source_id is None


def test_transient_retry_is_not_dispatched_as_supplement():
    rows=[task_result(search_executions=[failed_search(503)])]
    decisions,_=policy(rows)
    assert decisions[0].action==A.STOP
    again,_=policy(rows,history=decisions,round_number=2)
    assert again[0].action==A.STOP


def test_explicit_execution_budget_stops_despite_unread_artifact():
    failure=make_failure(C.BUDGET_EXHAUSTED,scope=FailureScope(point_id='NP-1'))
    decisions,_=policy([task_result(execution_failures=[failure],candidate_audit=[CandidateAuditRecord(
        namespace='research_reference',artifact_ids=['a'],status='not_read',reason='known')])])
    assert decisions[0].action==A.STOP
    assert failure.event_id in decisions[0].cause_event_ids


def test_count_gap_does_not_create_a_read_only_recovery_task():
    decisions,_=policy([task_result(candidate_audit=[CandidateAuditRecord(
        namespace='subject_reference',artifact_ids=['a'],status='not_read',reason='known')])])
    assert decisions[0].action==A.RESEARCH_GAP
    assert decisions[0].artifacts==[]


def test_excluded_target_never_becomes_recovery_material():
    decisions,_=policy([task_result(candidate_audit=[CandidateAuditRecord(namespace='research_reference',
        artifact_ids=['a'],status='excluded',reason='target',excluded_reason='target_paper')])])
    assert decisions[0].action==A.RESEARCH_GAP
    assert not decisions[0].artifacts


def test_completed_empty_search_remains_coverage_gap_with_causal_event():
    execution=SearchExecution.model_validate({**failed_search().model_dump(), 'status':'succeeded','failure':None,'error':None})
    decisions,failures=policy([task_result(search_executions=[execution])])
    event=next(f for f in failures if f.code==C.COVERAGE_NO_MATCH)
    query = next(d for d in decisions if d.trigger_codes == [C.COVERAGE_NO_MATCH.value])
    count = next(d for d in decisions if d.trigger_codes == [C.EVIDENCE_MISSING.value])
    assert query.action==A.REPLAN_QUERY
    assert event.event_id in query.cause_event_ids
    assert event.event_id not in count.cause_event_ids
    assert event.semantic_status=='not_adjudicated'


def test_semantic_gap_with_abstract_only_record_does_not_dispatch_fulltext():
    from novelty_agent_framework.schemas.references import ResearchBundle, SourceRecord
    bundle = ResearchBundle(bundle_id='bundle-1', producer='arxiv', source_records=[
        SourceRecord(source_record_id='record-1', source_id='arxiv',
                     source_kind='structured_database', title='Candidate', access_status='metadata_only',
                     observed_at=datetime.now(timezone.utc))])
    review = NoveltyPointReview(novelty_point_id='NP-1', status='insufficient_evidence',
        incomplete_reason='semantic_evidence', supplement_request=SupplementRequest(
            reason='Missing method evidence', missing_aspects=['mechanism']))
    decisions, failures = policy([task_result(research_bundles=[bundle])], reviews=[review])
    assert decisions[0].action == A.RESEARCH_GAP
    assert decisions[0].missing_aspects == ['mechanism']
    assert decisions[0].source_id is None
    assert decisions[0].source_record_ids == []
    assert any(f.code == C.REVIEW_MISSING_FEATURES for f in failures)


def test_task_supplement_main_path_calls_coordinator_and_uses_new_plans():
    workflow, _ = build_workflow(max_rounds=2)
    calls = []
    original = workflow.services.coordinator.plan_supplement
    def recording_call(*args, **kwargs):
        calls.append(kwargs)
        return original(*args, **kwargs)
    workflow.services.coordinator.plan_supplement = recording_call
    state = _final_evidence_sufficiency_state(0)
    state['search_plans'] = [scope().search_plan]
    checked = asyncio.run(workflow._check_final_evidence_sufficiency(state))
    assert checked['recovery_decisions'][0].action == A.RESEARCH_GAP
    updated = asyncio.run(workflow._plan_supplement({**state, **checked}))
    assert updated['research_tasks']
    assert len(calls) == 1
    assert calls[0]['recovery_decisions'] == checked['recovery_decisions']
    assert updated['recovery_decisions'][0].coordinator_status == 'planned'
    assert all(t.attempt == 2 for t in updated['research_tasks'])
    assert updated['recovery_search_plans'] == {}


class RecordingTool:
    description='test recorder'
    def __init__(self,name,args_schema,payload=None):
        self.name,self.args_schema,self.payload=name,args_schema,payload or {}
        self.calls=[]
    async def ainvoke(self,arguments,*,scope):
        self.calls.append((arguments,scope))
        return ResearcherToolObservation(tool_name=self.name,succeeded=True,payload=self.payload)


def recovery_scope(action,**kwargs):
    request=scope()
    return request.model_copy(update={'recovery':RecoveryDecision(point_id='NP-1',action=action,reason='bound recovery',**kwargs)})


def test_read_only_recovery_hides_search_and_rejects_wrong_namespace_or_scope():
    reader=RecordingTool('reader',ReaderCallArguments)
    reader._namespace_for=lambda paper,item:'subject_reference'
    database=RecordingTool('database_search',DatabaseSearchArguments)
    request=recovery_scope(A.READ_ARTIFACT,artifacts=[RecoveryArtifact(namespace='subject_reference',artifact_id='a')])
    registry=RecoveryToolRegistry(ResearcherToolRegistry([reader,database]),request)
    assert registry.names==('reader',)
    async def run():
        reader._namespace_for=lambda paper,item:'research_reference'
        denied=await registry.execute('reader',{'artifact_id':'a'},scope=request)
        reader._namespace_for=lambda paper,item:'subject_reference'
        assert not denied.succeeded and denied.payload['failure']['code']=='recovery.scope.changed'
        denied=await registry.execute('reader',{'artifact_id':'a'},scope=request.model_copy(update={'run_id':'other-run'}))
        assert not denied.succeeded
        allowed=await registry.execute('reader',{'artifact_id':'a'},scope=request)
        assert allowed.succeeded
    asyncio.run(run())
    assert len(reader.calls)==1 and not database.calls


def test_fulltext_recovery_blocks_repeat_search_and_only_unlocks_bound_artifacts():
    database=RecordingTool('database_search',DatabaseSearchArguments,{'artifacts':[
        {'artifact_id':'a','source_record_id':'r1'},{'artifact_id':'foreign','source_record_id':'r2'}]})
    reader=RecordingTool('reader',ReaderCallArguments)
    request=recovery_scope(A.FETCH_FULLTEXT,source_id='arxiv',source_record_ids=['r1'])
    registry=RecoveryToolRegistry(ResearcherToolRegistry([database,reader]),request)
    async def run():
        for args in ({'source_id':'arxiv'}, {'source_id':'other','full_text_source_record_ids':['r1']},
                     {'source_id':'arxiv','full_text_source_record_ids':['r2']}):
            assert not (await registry.execute('database_search',args,scope=request)).succeeded
        assert (await registry.execute('database_search',{'source_id':'arxiv','full_text_source_record_ids':['r1']},scope=request)).succeeded
        assert (await registry.execute('reader',{'artifact_id':'a'},scope=request)).succeeded
        assert not (await registry.execute('reader',{'artifact_id':'foreign'},scope=request)).succeeded
    asyncio.run(run())
    assert len(database.calls)==len(reader.calls)==1


def test_recovery_preserves_original_registry_policy_and_checkpoint_hooks():
    reader=RecordingTool('reader',ReaderCallArguments)
    class BoundRegistry(ResearcherToolRegistry):
        async def execute_validated(self,*args,**kwargs):
            return ResearcherToolObservation(tool_name='reader',succeeded=False,error='original policy refusal')
    request=recovery_scope(A.READ_ARTIFACT,artifacts=[RecoveryArtifact(artifact_id='a')])
    registry=RecoveryToolRegistry(BoundRegistry([reader]),request)
    result=asyncio.run(registry.execute('reader',{'artifact_id':'a'},scope=request))
    assert not result.succeeded and result.error=='original policy refusal'
    assert not reader.calls


@pytest.mark.parametrize('error_factory,expected',[
    (lambda: __import__('backend.env.model_client',fromlist=['ModelTransportTimeout']).ModelTransportTimeout('timeout'),C.MODEL_TIMEOUT),
    (lambda: __import__('backend.env.model_client',fromlist=['ModelCallBudgetExceeded']).ModelCallBudgetExceeded('budget'),C.MODEL_CALL_BUDGET),
    (lambda: __import__('backend.env.model_client',fromlist=['ModelContextAdmissionError']).ModelContextAdmissionError({'code':'CONTEXT_LIMIT_EXCEEDED'}),C.MODEL_CONTEXT_LIMIT),
])
def test_failed_research_keeps_structured_execution_cause(error_factory,expected):
    class FailingModel:
        async def acomplete(self,*args,**kwargs): raise error_factory()
    result=asyncio.run(TaskResearcherWorkflow(FailingModel(),ResearcherToolRegistry(),FakeBuilder()).ainvoke(scope()))
    assert result.status.value=='partial'
    assert result.execution_failures[0].code==expected
    assert not result.evidence_cards


def test_local_provider_directive_cannot_enter_coordinator_supplement():
    workflow,_=build_workflow(max_rounds=2)
    state=_final_evidence_sufficiency_state(0)
    request=scope()
    state.update(all_research_tasks=[request.research_task],search_plans=[request.search_plan],
        recovery_decisions=[RecoveryDecision(point_id='NP-1',action=A.CHANGE_PROVIDER,source_id='openalex',reason='failed')])
    updated=asyncio.run(workflow._plan_supplement(state))
    assert updated['research_tasks']==[]
    assert updated['recovery_search_plans']=={}
    assert 'rounds' not in updated


def test_runtime_routing_matches_recovery_when_card_count_passes():
    state=_final_evidence_sufficiency_state(1)
    decision=RecoveryDecision(point_id='NP-1',action=A.RESEARCH_GAP,reason='missing feature')
    output={'insufficient_final_evidence_points':[],'recovery_decisions':[decision]}
    details=_stage_debug_details('check_final_evidence_sufficiency',stage_input=state,stage_output=output,
        runtime_config={'workflow':{'min_final_evidence_cards_per_point':1,'max_rounds':2}})
    assert details['final_evidence_sufficiency']['round_limit_allows_supplement'] is True
    assert details['final_evidence_sufficiency']['routing_basis']=='recovery_decisions'


def test_report_keeps_partial_review_and_stop_reason_without_promoting_verdict():
    workflow,_=build_workflow(max_rounds=2)
    state=_final_evidence_sufficiency_state(1)
    point=state['brief'].novelty_points[0]
    partial=NoveltyPointReview(novelty_point_id=point.point_id,status='insufficient_evidence',
        incomplete_reason='semantic_evidence',verdict_reason='original local fact')
    state.update(novelty_points=[point],novelty_reviews=[partial],
        review_summary_attempts=[{'point_id':point.point_id,'status':'failed','review':{'incomplete_reason':'technical_error'},'partial_card_results':[
            {'card_id':'CARD-0','status':'completed','review':partial.model_dump(mode='json')},
            {'card_id':'C2','status':'failed','review':None}]}],
        recovery_decisions=[RecoveryDecision(point_id=point.point_id,action=A.STOP,reason='provider technically blocked',max_additional_attempts=0)],
        failure_events=[failed_search().failure],point_coverage={'semantic_coverage_verified':False})
    report=asyncio.run(workflow._synthesize_report(state))['report']
    assert len(report.partial_card_reviews)==1
    assert report.partial_card_reviews[0].review.verdict_reason=='original local fact'
    assert report.conclusions[0].verdict is None
    assert report.execution_failures[0].code==C.PROVIDER_PROTOCOL
    assert report.point_coverage['semantic_coverage_verified'] is False
    assert report.point_lifecycle[0]['report_present'] is True
    assert 'task_not_created' in report.point_lifecycle[0]['gaps']
    assert any('provider technically blocked' in item for item in report.limitations)


def test_lifecycle_distinguishes_missing_planning_and_missing_execution_from_no_evidence():
    from novelty_agent_framework.core.report_binding import bind_point_lifecycle_to_report
    from novelty_agent_framework.schemas import NoveltyReport
    request=scope()
    report=bind_point_lifecycle_to_report(NoveltyReport(paper_id='paper-1'),state={
        'novelty_points':[request.novelty_point], 'all_research_tasks':[request.research_task]})
    row=report.point_lifecycle[0]
    assert row['gaps']==['search_plan_missing','task_result_missing','validated_evidence_missing','review_missing','report_conclusion_missing']
    assert row['semantic_scope_complete'] is None


def test_old_read_recovery_history_does_not_select_supplement_actions():
    row=task_result(candidate_audit=[CandidateAuditRecord(namespace='research_reference',artifact_ids=['a'],status='not_read',reason='known')])
    first=[RecoveryDecision(point_id='NP-1',action=A.READ_ARTIFACT,reason='legacy read',
        artifacts=[RecoveryArtifact(artifact_id='a')])]
    second,_=policy([row],history=first,round_number=2)
    assert second[0].action==A.RESEARCH_GAP
    assert second[0].artifacts==[]


def test_reviewer_technical_failure_with_cards_precedes_unread_or_provider_research():
    row=task_result(evidence_cards=[c.model_copy(update={'evidence_ids':[]}) for c in _final_evidence_sufficiency_state(1)['evidence_cards']],
        candidate_audit=[CandidateAuditRecord(namespace='research_reference',artifact_ids=['a'],status='not_read',reason='known')],
        search_executions=[failed_search()])
    review=NoveltyPointReview(novelty_point_id='NP-1',status='insufficient_evidence',incomplete_reason='technical_error')
    decisions,_=policy([row],reviews=[review],provider_order=('arxiv','openalex'))
    assert decisions[0].action==A.STOP
    assert 'Reviewer' in decisions[0].reason


def test_nondurable_completed_summary_is_not_reported_as_failed_summary():
    workflow,_=build_workflow(max_rounds=2)
    state=_final_evidence_sufficiency_state(0)
    point=state['brief'].novelty_points[0]
    local=NoveltyPointReview(novelty_point_id=point.point_id,status='insufficient_evidence',incomplete_reason='semantic_evidence')
    state.update(novelty_points=[point],novelty_reviews=[local],review_summary_attempts=[{
        'point_id':point.point_id,'status':'checkpoint_unavailable','review':local.model_dump(mode='json'),
        'partial_card_results':[{'card_id':'C1','status':'completed','review':local.model_dump(mode='json')}]}])
    report=asyncio.run(workflow._synthesize_report(state))['report']
    assert report.partial_card_reviews==[]
    assert not any('汇总未完成' in item for item in report.limitations)


def test_recovery_history_cause_ids_remain_present_across_rounds():
    workflow,_=build_workflow(max_rounds=3)
    state=_final_evidence_sufficiency_state(0)
    first=asyncio.run(workflow._check_final_evidence_sufficiency(state))
    old_ids={f.event_id for f in first['failure_events']}
    second=asyncio.run(workflow._check_final_evidence_sufficiency({**state,**first,'rounds':2}))
    assert old_ids.issubset({f.event_id for f in second['failure_events']})


def test_partial_report_cannot_reintroduce_card_removed_by_final_gate():
    workflow,_=build_workflow(max_rounds=2)
    state=_final_evidence_sufficiency_state(0)
    point=state['brief'].novelty_points[0]
    local=NoveltyPointReview(novelty_point_id=point.point_id,status='insufficient_evidence',incomplete_reason='semantic_evidence')
    state.update(novelty_points=[point],novelty_reviews=[local],review_summary_attempts=[{
        'point_id':point.point_id,'status':'failed','review':{'incomplete_reason':'technical_error'},
        'partial_card_results':[{'card_id':'REMOVED','status':'completed','review':local.model_dump(mode='json')}]}])
    report=asyncio.run(workflow._synthesize_report(state))['report']
    assert report.partial_card_reviews==[]


def test_provider_search_recovery_cannot_change_action_to_fulltext_acquisition():
    database=RecordingTool('database_search',DatabaseSearchArguments)
    request=recovery_scope(A.RETRY_REQUEST,source_id='arxiv')
    registry=RecoveryToolRegistry(ResearcherToolRegistry([database]),request)
    result=asyncio.run(registry.execute('database_search',{'source_id':'arxiv','full_text_source_record_ids':['r']},scope=request))
    assert not result.succeeded and result.payload['failure']['code']=='recovery.scope.changed'
    assert not database.calls


def test_fulltext_http_failure_preserves_cause_and_original_abstract(tmp_path):
    from test_structured_retrieval_tool import request, hit, DemoQueryAdapter
    from novelty_agent_framework.tools.database_search import RetrievalSource, StructuredSourceRetrievalTool
    from novelty_agent_framework.persistence import ReferenceStore
    class Search:
        source_id='demo'
        def search(self,*args,**kwargs): return [hit()]
    class FullText:
        source_id='demo'
        def fetch(self,*args,**kwargs):
            response=httpx.Response(403,request=httpx.Request('GET','https://example.test/paper'))
            raise httpx.HTTPStatusError('failed',request=response.request,response=response)
    source=RetrievalSource(source_id='demo',query_adapter=DemoQueryAdapter(),search_tool=Search(),full_text_tool=FullText())
    bundle=asyncio.run(StructuredSourceRetrievalTool(source=source,reference_store=ReferenceStore(tmp_path)).ainvoke(request()))
    assert any(a.role.value=='abstract' for a in bundle.artifacts)
    cause=next(f for f in bundle.execution_failures if f.code==C.PROVIDER_AUTHORIZATION)
    material=next(f for f in bundle.execution_failures if f.code==C.FULLTEXT_FAILED)
    assert material.cause_event_ids==[cause.event_id]
    assert any(e.results for e in bundle.search_executions)
    assert all(f.semantic_status=='not_adjudicated' for f in bundle.execution_failures)


def test_wrong_tool_choice_is_returned_to_model_and_classified_without_aborting_normalization():
    from backend.env import ModelResponse,ModelToolCall
    model=FakeModel([ModelResponse(content=None,tool_calls=(ModelToolCall('bad','unregistered',{}),)),finish()])
    result=asyncio.run(TaskResearcherWorkflow(model,ResearcherToolRegistry(),FakeBuilder()).ainvoke(scope()))
    assert len(model.messages)==2
    assert result.status.value=='completed'
    assert any(f.code==C.TOOL_NOT_REGISTERED for f in result.execution_failures)
    assert 'tool.dispatch.not_registered' in model.messages[1][-1].content


def test_invalid_tool_arguments_have_a_distinct_code_and_never_execute():
    database=RecordingTool('database_search',DatabaseSearchArguments)
    registry=ResearcherToolRegistry([database])
    result=asyncio.run(registry.execute('database_search',{},scope=scope()))
    assert result.payload['failure']['code']=='tool.arguments.schema_invalid'
    assert registry.project_model_context('database_search',result)['failure']['code']=='tool.arguments.schema_invalid'
    assert not database.calls


def test_406_does_not_enter_supplement_even_with_configured_alternative():
    workflow, _ = build_workflow(max_rounds=2, recovery_provider_order=('arxiv', 'openalex'))
    state = _final_evidence_sufficiency_state(0)
    original = scope()
    failure = failed_search()
    state.update(all_research_tasks=[original.research_task], search_plans=[original.search_plan],
                 task_research_results=[task_result(search_executions=[failure])])
    checked = asyncio.run(workflow._check_final_evidence_sufficiency(state))
    combined = {**state, **checked}
    assert asyncio.run(workflow._route_after_evidence_sufficiency_check(combined)) == 'synthesize'
    decision = checked['recovery_decisions'][0]
    assert decision.action == A.STOP and decision.source_id is None
    assert failure.failure.event_id in decision.cause_event_ids
    updated = asyncio.run(workflow._plan_supplement(combined))
    assert updated['research_tasks'] == []
    assert updated['recovery_search_plans'] == {}


def test_explicit_provider_recovery_registry_still_enforces_target():
    # Registry capability remains available to local correction implementations;
    # plan_recovery no longer emits this directive.
    request = recovery_scope(A.CHANGE_PROVIDER, source_id='openalex')
    database = RecordingTool('database_search', DatabaseSearchArguments)
    registry = RecoveryToolRegistry(ResearcherToolRegistry([database]), request)

    async def execute():
        denied = await registry.execute('database_search', {'source_id': 'arxiv'}, scope=request)
        assert not denied.succeeded
        allowed = await registry.execute('database_search', {'source_id': 'openalex'}, scope=request)
        assert allowed.succeeded
    asyncio.run(execute())
    assert len(database.calls) == 1
    assert database.calls[0][0].source_id == 'openalex'
