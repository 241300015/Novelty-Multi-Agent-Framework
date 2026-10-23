"""Issue #22: real stored material, bounded node-local repair, and failure history."""
import asyncio
import hashlib
import json
from dataclasses import replace

import pytest
from backend.env import ModelResponse, ModelToolCall
from novelty_agent_framework.agents import DefaultEvidenceValidator, DemoQueryAdapter
from novelty_agent_framework.core import local_correction as local
from novelty_agent_framework.core.runtime_artifacts import RuntimeArtifactManager, RuntimeDebugConfig
from novelty_agent_framework.core.tool_call_harness import ToolCallHarnessEvent
from novelty_agent_framework.schemas import (
    DatabaseSearchArguments, ReaderArguments, ResearcherToolObservation,
    ResearchFinishDraft, TaskResearchResult, TaskResearchStatus,
)
from novelty_agent_framework.schemas.failures import FailureCode as C, FailureScope, make_failure
from novelty_agent_framework.tools import EvidenceCardBuilder, ResearcherToolRegistry
from novelty_agent_framework.tools.reader import ReaderTool
from novelty_agent_framework.tools.reference_reader import ReferenceArtifactReaderTool
from novelty_agent_framework.tools.database_search import DatabaseSearchTool, RetrievalSource, StructuredSourceRetrievalTool
from novelty_agent_framework.workflows import TaskResearcherWorkflow, TaskResearcherConfig
from novelty_agent_framework.workflows.research_task import _trusted_reads
from novelty_agent_framework.workflows.research_reading_policy import ResearchReadingPolicy
from test_evidence_card_builder import prepare_store, scope, card, quote, TEXT_A, TEXT_B
from test_tool_call_harness import ScriptedModelClient
from test_card_recovery import call, finish
from test_database_search_tool import StaticRetrieval, bundle, SearchExecutionStatus as S


def real_store(tmp_path):
    store = prepare_store(tmp_path)
    manifest = store.load_manifest('paper-1')
    artifacts = []
    for artifact, text in zip(manifest.artifacts, (TEXT_A, TEXT_B)):
        store.write_document('paper-1', work_id=artifact.work_id, artifact_id=artifact.artifact_id,
                             extension='txt', content=text)
        artifacts.append(artifact.model_copy(update={'sha256': hashlib.sha256(text.encode()).hexdigest(),
                                                     'byte_size':len(text.encode())}))
    store.persist_manifest('paper-1', manifest.model_copy(update={'artifacts':artifacts}))
    return store


def reader(store):
    return ReaderTool(ReferenceArtifactReaderTool(store))


def task(store, model, **config):
    return TaskResearcherWorkflow(model, ResearcherToolRegistry([reader(store)]),
                                 EvidenceCardBuilder(store), config=TaskResearcherConfig(**config))


@pytest.mark.parametrize('bad', [None, 1, 'text', {'artifact_id': 'x'}])
def test_malformed_read_collection_keeps_other_verified_observations(tmp_path, bad):
    store = real_store(tmp_path)
    observed = asyncio.run(reader(store).ainvoke(ReaderArguments(artifact_id='art_a'), scope=scope()))
    trace = [ToolCallHarnessEvent(kind='tool_result', observation=ResearcherToolObservation(
        tool_name='reader', succeeded=True, payload={'read_results':bad})),
        ToolCallHarnessEvent(kind='tool_result', observation=observed)]
    reads, warnings = _trusted_reads(trace)
    assert len(reads) == 1 and warnings


@pytest.mark.parametrize('budget', [0, 1])
def test_reader_range_correction_is_bounded_and_audited(tmp_path, budget):
    store = real_store(tmp_path)
    session = local.LocalSession(model_limit=0, tool_limit=budget, read_limit=100)
    manager = RuntimeArtifactManager('paper-1', run_id='run-builder', config=RuntimeDebugConfig(
        output_root=tmp_path/'runtime', archive_root=tmp_path/'archive'))
    with manager, session.bind():
        obs = asyncio.run(reader(store).ainvoke(ReaderArguments(artifact_id='art_a',char_start=999),scope=scope()))
    assert obs.succeeded == bool(budget)
    records = list(session.records.values())
    assert len(records) == 1
    assert records[0].status.value == ('verification_passed' if budget else 'stopped')
    assert list(manager.run_dir.glob('corrections/**/*.json'))
    if budget:
        assert obs.payload['read_result']['char_start'] == 0
        assert obs.payload['read_result']['text'] == TEXT_A


def test_reader_eof_does_not_retry_and_corrupt_file_stops(tmp_path):
    store = real_store(tmp_path)
    session = local.LocalSession(model_limit=0,tool_limit=2,read_limit=100)
    with session.bind():
        obs = asyncio.run(reader(store).ainvoke(ReaderArguments(artifact_id='art_a',char_start=len(TEXT_A)),scope=scope()))
        assert obs.payload['read_status'] == 'artifact_eof' and not session.records
        store.verify_artifact_file('paper-1','art_a')[1].write_text('tampered')
        with pytest.raises(ValueError):
            asyncio.run(reader(store).ainvoke(ReaderArguments(artifact_id='art_a'),scope=scope()))
    assert all(r.status.value == 'stopped' for r in session.records.values())


def test_reading_policy_retries_before_consuming_candidate():
    registry = ResearcherToolRegistry([])
    # Menus require a registered reader but this test only exercises policy, no I/O.
    class Tool:
        name='reader'
    registry.register(Tool())
    policy = ResearchReadingPolicy(registry, scope())
    policy.bodies={'art_a':'read'}
    call_ = ModelToolCall('r','reader',{'artifact_id':'art_a','char_start':0})
    failed = ResearcherToolObservation(tool_name='reader',succeeded=False,payload={'failure_code':C.READER_FILE_MISSING.value})
    policy.observe(call_, failed)
    assert 'art_a' in policy.bodies and 'art_a' not in policy.consumed
    assert any(a.name=='reader' for a in policy.actions.values())
    policy.observe(call_, failed)
    assert 'art_a' not in policy.bodies and 'art_a' in policy.failed_reads


@pytest.mark.parametrize('success', [True,False])
def test_source_change_preserves_original_execution_and_terminal_record(tmp_path,success):
    store=real_store(tmp_path)
    original=bundle(S.FAILED)
    event=make_failure(C.PROVIDER_AUTHENTICATION,scope=FailureScope(provider='demo'),message='authentication failed')
    original=original.model_copy(update={'search_executions':[original.search_executions[0].model_copy(update={'failure':event})]})
    alt=bundle(S.SUCCEEDED if success else S.FAILED)
    second=StaticRetrieval(alt,store);second.source_id='demo2'
    tool=DatabaseSearchTool({'demo':StaticRetrieval(original,store),'demo2':second},store)
    session=local.LocalSession(model_limit=0,tool_limit=2,read_limit=100)
    with session.bind():
        obs=asyncio.run(tool.ainvoke(DatabaseSearchArguments(source_id='demo'),scope=scope()))
    assert obs.succeeded is success
    assert len(obs.payload['search_executions'])==2
    assert obs.payload['source_change']=={'from':'demo','to':'demo2'}
    assert list(session.records.values())[-1].status.value==('verification_passed' if success else 'stopped')


class FlakySearch:
    source_id='demo'
    def __init__(self, permanent=False):
        self.calls=[];self.permanent=permanent
    def search(self, query, *,limit=10):
        self.calls.append(query)
        if self.permanent or len(self.calls)==1:
            raise TimeoutError('transient')
        return []


@pytest.mark.parametrize('permanent', [False,True])
def test_task_end_retry_only_replays_failed_queries(tmp_path,permanent):
    store=real_store(tmp_path);backend=FlakySearch(permanent)
    retrieval=StructuredSourceRetrievalTool(source=RetrievalSource(source_id='demo',
        query_adapter=DemoQueryAdapter(),search_tool=backend),reference_store=store)
    db=DatabaseSearchTool({'demo':retrieval},store)
    model=ScriptedModelClient(ModelResponse(content=None,tool_calls=[ModelToolCall('search','database_search',{'source_id':'demo'})]),
        ModelResponse(content=ResearchFinishDraft(cards=[],no_evidence_reason='search failed').model_dump_json()))
    workflow=TaskResearcherWorkflow(model,ResearcherToolRegistry([db]),EvidenceCardBuilder(store))
    result=asyncio.run(workflow.ainvoke(scope()))
    assert len(backend.calls)==2 and backend.calls[0]==backend.calls[1]
    assert len(model.calls)==2
    assert any(e.status.value=='failed' for e in result.search_executions)
    record=next(r for r in result.correction_records if r.owner_node=='TaskResearcherWorkflow.ainvoke')
    assert record.status.value==('stopped' if permanent else 'verification_passed')
    assert result.correction_budget['tool_used']==2


@pytest.mark.parametrize('success', [True,False])
def test_source_confusion_has_real_revalidation_and_preserves_valid_card(tmp_path,success):
    store=real_store(tmp_path)
    request=scope().model_copy(update={'novelty_point':scope().novelty_point.model_copy(update={'claim':'Target invented quote.'})})
    model=ScriptedModelClient(call('a'),call('b','art_b'),
        finish(card(quote('Alpha unique quote.')),card(quote('Target invented quote.'))),
        finish(card(quote('Beta unique quote.' if success else 'Target invented quote.'))))
    result=asyncio.run(task(store,model).ainvoke(request))
    assert len(result.evidence_cards)==(2 if success else 1)
    assert any(e.quote=='Alpha unique quote.' for e in result.evidence)
    checked=DefaultEvidenceValidator().validate(result.evidence_cards,tasks=[request.research_task],
        evidence=result.evidence,novelty_points=[request.novelty_point],paper_id='paper-1',reference_store=store)
    assert not checked.rejected
    record=next(r for r in result.correction_records if r.owner_node.endswith('_correct_rejected_cards'))
    assert record.status.value==('verification_passed' if success else 'stopped')
    assert json.loads(model.calls[-1][0][-1].content)['source_boundary']['target_input_is_not_reference']


@pytest.mark.parametrize('success', [True,False])
def test_validator_round_trip_preserves_budget_and_good_card(tmp_path,success):
    store=real_store(tmp_path)
    model=ScriptedModelClient(call('a'),call('b','art_b'),
        finish(card(quote('Alpha unique quote.')),card(quote('Beta unique quote.'))),
        finish(card(quote('Beta unique quote.' if success else 'invented'))))
    workflow=task(store,model);request=scope()
    result=asyncio.run(workflow.ainvoke(request))
    good,bad=result.evidence_cards
    bad=bad.model_copy(update={'document_title':'wrong title'})
    result=result.model_copy(update={'evidence_cards':[good,bad]})
    validator=DefaultEvidenceValidator()
    args=dict(tasks=[request.research_task],evidence=result.evidence,novelty_points=[request.novelty_point],
              paper_id='paper-1',reference_store=store)
    rejected=validator.validate(result.evidence_cards,**args).rejected
    corrected=asyncio.run(workflow.correct_validation(request,result,rejected,validator=validator,
        tasks=[request.research_task],points=[request.novelty_point],reference_store=store))
    assert good in corrected.evidence_cards
    assert corrected.correction_budget['model_used']==result.correction_budget['model_used']+1
    check=validator.validate(corrected.evidence_cards,**{**args,'evidence':corrected.evidence})
    assert len(check.accepted)==(2 if success else 1)
    again=asyncio.run(workflow.correct_validation(request,corrected,rejected,validator=validator,
        tasks=[request.research_task],points=[request.novelty_point],reference_store=store))
    assert again.correction_budget==corrected.correction_budget and len(model.calls)==4
    assert next(r for r in corrected.correction_records if r.owner_node.endswith('correct_validation')).status.value==('verification_passed' if success else 'stopped')


def test_validator_workflow_replaces_versions_without_supplement_round(tmp_path):
    from test_workflow import build_workflow, make_paper
    from novelty_agent_framework.persistence import persist_workflow_input
    from langgraph.types import Overwrite
    store=real_store(tmp_path)
    model=ScriptedModelClient(call('a'),finish(card(quote('Alpha unique quote.'))),finish(card(quote('Alpha unique quote.'))))
    researcher=task(store,model);request=scope()
    result=asyncio.run(researcher.ainvoke(request))
    bad=result.evidence_cards[0].model_copy(update={'document_title':'bad title'})
    result=result.model_copy(update={'evidence_cards':[bad]})
    workflow,_=build_workflow(researcher=researcher,validator=DefaultEvidenceValidator(),output_root=tmp_path)
    paper=make_paper().model_copy(update={'paper_id':'paper-1'})
    persist_workflow_input(paper,output_root=tmp_path,run_id=request.run_id)
    state={'paper':paper,'run_id':request.run_id,'rounds':1,'all_research_tasks':[request.research_task],
        'search_plans':[request.search_plan],'novelty_points':[request.novelty_point],
        'task_research_results':[result],'raw_evidence_cards':[bad],'raw_evidence':result.evidence}
    updated=asyncio.run(workflow._validate_evidence(state))
    assert len(updated['evidence_cards'])==1 and updated['evidence_cards'][0].document_title!='bad title'
    assert isinstance(updated['raw_evidence_cards'],Overwrite)
    assert len(updated['raw_evidence_cards'].value)==1
    assert 'rounds' not in updated and state['rounds']==1
    assert updated['evidence_integrity']['initial_rejections'][0]['card_id']==bad.card_id
    assert updated['novelty_reviews']==[]


def test_interrupted_correction_retains_budget_and_does_not_replay(tmp_path):
    manager=RuntimeArtifactManager('paper-1',run_id='run-builder',config=RuntimeDebugConfig(
        output_root=tmp_path/'out',archive_root=tmp_path/'archive'))
    with manager:
        first=local.LocalSession(model_limit=2,tool_limit=2,read_limit=100)
        first.attach(scope())
        with pytest.raises(RuntimeError),first.bind():
            assert first.once('validator:card-1')
            first.charge('model')
            record=local.detect(scope(),C.MODEL_OUTPUT_REFERENCE,'test',message='rejected')
            local.begin(record, __import__('novelty_agent_framework.schemas.failures',fromlist=['RecoveryAction']).RecoveryAction.REPAIR_OUTPUT)
            raise RuntimeError('interrupted')
        recovered=local.LocalSession(model_limit=2,tool_limit=2,read_limit=100)
        recovered.attach(scope())
        assert recovered.model_used==1 and not recovered.once('validator:card-1')
        assert all(r.status.value=='stopped' for r in recovered.records.values())
        assert len(list(manager.run_dir.glob('local_correction_budgets/*.json')))==1


def test_validator_budget_exhaustion_preserves_card_and_does_not_call_model(tmp_path):
    store=real_store(tmp_path);model=ScriptedModelClient(call('a'),finish(card(quote('Alpha unique quote.'))))
    workflow=task(store,model);request=scope()
    result=asyncio.run(workflow.ainvoke(request))
    damaged=result.evidence_cards[0].model_copy(update={'document_title':'wrong'})
    result=result.model_copy(update={'evidence_cards':[damaged],
        'correction_budget':{**result.correction_budget,'model_used':result.correction_budget['model_limit']}})
    corrected=asyncio.run(workflow.correct_validation(request,result,[(damaged.card_id,'title mismatch')],
        validator=DefaultEvidenceValidator(),tasks=[request.research_task],points=[request.novelty_point],reference_store=store))
    assert len(model.calls)==2 and corrected.evidence_cards==[damaged]
    record=corrected.correction_records[-1]
    assert record.status.value=='stopped' and record.attempt==0


@pytest.mark.parametrize('recover', [True,False])
def test_whole_builder_io_failure_gets_one_rebuild(tmp_path,recover):
    store=real_store(tmp_path)
    class Builder(EvidenceCardBuilder):
        calls=0
        def build(self,*args,**kwargs):
            self.calls+=1
            if self.calls==1 or not recover:
                raise OSError('temporary manifest I/O')
            return super().build(*args,**kwargs)
    builder=Builder(store)
    model=ScriptedModelClient(call('a'),finish(card(quote('Alpha unique quote.'))))
    workflow=TaskResearcherWorkflow(model,ResearcherToolRegistry([reader(store)]),builder)
    result=asyncio.run(workflow.ainvoke(scope()))
    assert builder.calls==2
    assert bool(result.evidence_cards)==recover and result.read_results
    assert result.correction_records[-1].status.value==('verification_passed' if recover else 'stopped')


def test_provider_http_retries_and_task_end_retry_share_physical_cap(tmp_path):
    import httpx
    from novelty_agent_framework.tools.database_search.providers.common import HttpRequestPolicy, ResilientHttpClient
    from novelty_agent_framework.core.runtime_artifacts import current_runtime_artifacts
    sends=[]
    client=httpx.Client(transport=httpx.MockTransport(lambda request:(sends.append(request) or httpx.Response(503,request=request))))
    # Use the production HTTP retry layer; no external network.
    http=ResilientHttpClient(client,policy=HttpRequestPolicy(max_retries=2,retry_backoff_seconds=0))
    class Backend:
        source_id='demo'
        def search(self,query,*,limit=10):
            http.get('https://provider.test/search').raise_for_status()
            return []
    store=real_store(tmp_path)
    retrieval=StructuredSourceRetrievalTool(source=RetrievalSource(source_id='demo',query_adapter=DemoQueryAdapter(),search_tool=Backend()),reference_store=store)
    model=ScriptedModelClient(ModelResponse(content=None,tool_calls=[ModelToolCall('s','database_search',{'source_id':'demo'})]),
        ModelResponse(content=ResearchFinishDraft(cards=[],no_evidence_reason='failed').model_dump_json()))
    workflow=TaskResearcherWorkflow(model,ResearcherToolRegistry([DatabaseSearchTool({'demo':retrieval},store)]),EvidenceCardBuilder(store))
    manager=RuntimeArtifactManager('paper-1',run_id='run-builder',config=RuntimeDebugConfig(
        output_root=tmp_path/'out',archive_root=tmp_path/'archive',max_physical_provider_requests=4))
    with manager:
        result=asyncio.run(workflow.ainvoke(scope()))
    assert len(sends)==4  # 3 first-attempt HTTP sends + 1 task-end send, then hard stop.
    assert len(list((manager.run_dir/'provider_budget').glob('*.json')))==4
    assert any(e.parameters.get('not_run_reason')=='retrieval_incomplete_budget' for e in result.search_executions)


@pytest.fixture(autouse=True)
def persisted_correction_runtime(tmp_path):
    manager=RuntimeArtifactManager('paper-1',run_id='run-builder',config=RuntimeDebugConfig(
        output_root=tmp_path/'audit',archive_root=tmp_path/'audit-archive'))
    with manager:
        yield manager


def test_no_alternative_source_is_terminal_without_extra_io(tmp_path):
    store=real_store(tmp_path);value=bundle(S.FAILED)
    failure=make_failure(C.PROVIDER_AUTHENTICATION,scope=FailureScope(provider='demo'),message='denied')
    value=value.model_copy(update={'search_executions':[value.search_executions[0].model_copy(update={'failure':failure})]})
    tool=DatabaseSearchTool({'demo':StaticRetrieval(value,store)},store)
    obs=asyncio.run(tool.ainvoke(DatabaseSearchArguments(source_id='demo'),scope=scope()))
    assert not obs.succeeded and obs.payload['correction']['attempt']==0
    assert obs.payload['correction']['status']=='stopped'


def test_per_tool_limit_and_correction_model_reserve_cannot_be_bypassed(tmp_path):
    store=real_store(tmp_path)
    model=ScriptedModelClient(call('a'),finish(card(quote('invented'))))
    result=asyncio.run(task(store,model,max_correction_model_calls=0).ainvoke(scope()))
    assert len(model.calls)==2 and not result.evidence_cards
    assert result.card_correction_failure.code==C.BUDGET_TURNS
    assert result.correction_records[-1].attempt==0
    session=local.LocalSession(model_limit=1,tool_limit=3,read_limit=10,per_tool_limits={'database_search':1})
    session.charge_tool('database_search')
    with pytest.raises(ValueError,match='per-tool'):
        session.charge_tool('database_search')
    assert session.tool_used==1


def test_successful_queries_not_replayed_when_other_strategy_fails(tmp_path):
    from novelty_agent_framework.schemas import SearchStrategy
    class Backend:
        source_id='demo'
        def __init__(self): self.calls=[]
        def search(self,query,*,limit=10):
            self.calls.append(query)
            if 'otherterm' in query and self.calls.count(query)==1: raise TimeoutError('temporary')
            return []
    store=real_store(tmp_path);backend=Backend();request=scope()
    # Two independent queries; only the second one should be replayed.
    from novelty_agent_framework.schemas import SearchConcept
    plan=request.search_plan.model_copy(update={'concepts':[
        SearchConcept(concept_id='C1',name='one',terms=['firstterm']),
        SearchConcept(concept_id='C2',name='two',terms=['otherterm'])], 'strategies':[
        SearchStrategy(strategy_id='S1',level='broad',expression='C1'),
        SearchStrategy(strategy_id='S2',level='broad',expression='C2')]})
    request=request.model_copy(update={'search_plan':plan})
    tool=StructuredSourceRetrievalTool(source=RetrievalSource(source_id='demo',query_adapter=DemoQueryAdapter(),search_tool=backend),reference_store=store)
    model=ScriptedModelClient(ModelResponse(content=None,tool_calls=[ModelToolCall('s','database_search',{'source_id':'demo'})]),
        ModelResponse(content=ResearchFinishDraft(cards=[],no_evidence_reason='no evidence').model_dump_json()))
    result=asyncio.run(TaskResearcherWorkflow(model,ResearcherToolRegistry([DatabaseSearchTool({'demo':tool},store)]),EvidenceCardBuilder(store)).ainvoke(request))
    assert len(backend.calls)==3 and backend.calls[1]==backend.calls[2] and backend.calls[0]!=backend.calls[1]
    assert len([e for e in result.search_executions if e.status.value != "not_run"])==3


def test_retry_new_material_is_read_built_and_validated(tmp_path):
    from novelty_agent_framework.ports import SearchHit
    class Backend(FlakySearch):
        def search(self,query,*,limit=10):
            super().search(query,limit=limit)
            return [SearchHit(document_id='p1',source_id='demo',title='Recovered reference',abstract=TEXT_A,url='https://reference.test/p1')]
    store=real_store(tmp_path);backend=Backend()
    tool=StructuredSourceRetrievalTool(source=RetrievalSource(source_id='demo',query_adapter=DemoQueryAdapter(),search_tool=backend),reference_store=store)
    model=ScriptedModelClient(ModelResponse(content=None,tool_calls=[ModelToolCall('s','database_search',{'source_id':'demo'})]),
        ModelResponse(content=ResearchFinishDraft(cards=[],no_evidence_reason='no evidence').model_dump_json()),
        finish(card(quote('Alpha unique quote.'))))
    workflow=TaskResearcherWorkflow(model,ResearcherToolRegistry([DatabaseSearchTool({'demo':tool},store),reader(store)]),EvidenceCardBuilder(store))
    request=scope();result=asyncio.run(workflow.ainvoke(request))
    assert result.read_results and len(result.evidence_cards)==1
    checked=DefaultEvidenceValidator().validate(result.evidence_cards,tasks=[request.research_task],evidence=result.evidence,
        novelty_points=[request.novelty_point],paper_id='paper-1',reference_store=store)
    assert len(checked.accepted)==1 and not checked.rejected
