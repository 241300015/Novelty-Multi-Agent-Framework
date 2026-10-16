"""Fixed-input local-model comparison of original, split/menu and choice-only Researcher.

Preparation captures original NP/Task/Plan and one fixed source corpus. Each run
uses real model calls, actual disk Reader and the same current Validator. Database
responses are frozen to separate model/Harness changes from provider variability.
"""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from dataclasses import asdict, replace
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import sys
import time
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'backend/src')]

from backend.env import PromptLibrary
from backend.env.model_client import _load_dev_env
from novelty_agent_framework.agents import DefaultEvidenceValidator
from novelty_agent_framework.config.factory import build_workflow
from novelty_agent_framework.config.loader import load_application_config
from novelty_agent_framework.config.experiment import prepare_startup
from novelty_agent_framework.core.runtime_artifacts import RuntimeArtifactManager, RuntimeDebugConfig
from novelty_agent_framework.persistence import ReferenceStore
from novelty_agent_framework.schemas import DatabaseSearchArguments, ResearchBundle, TaskResearchRequest, TaskResearchResult
from novelty_agent_framework.tools.database_search.tool import DatabaseSearchTool

BASE = ROOT / 'outputs/validator-gate-a-local-20261016/runs/0001'
MENU = ROOT / 'outputs/guided-reading-20261016/runs/0001'
DEFAULT = ROOT / 'outputs/harness-comparison-20261016'
PAPER = 'MF2033k6lC'
PACKAGE = Path('backend/src/novelty_agent_framework')
ARMS = ('original', 'split_menu', 'choice_only')


def read(path): return json.loads(path.read_text())
def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str) + '\n')
def digest(path): return hashlib.sha256(path.read_bytes()).hexdigest()
def canonical(value): return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def load_module(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def variant(root, arm):
    source = root / 'fixture/versions' / arm
    if arm == 'choice_only':
        from novelty_agent_framework.workflows import research_task
        from novelty_agent_framework.tools import evidence_card_builder
        return research_task, evidence_card_builder, ROOT / PACKAGE / 'prompts'
    harness = load_module(source / PACKAGE / 'core/tool_call_harness.py',
                         f'novelty_agent_framework.core._experiment_{arm}')
    task = load_module(source / PACKAGE / 'workflows/research_task.py',
                       f'novelty_agent_framework.workflows._experiment_{arm}')
    task.ToolCallHarness = harness.ToolCallHarness
    task.ToolCallHarnessConfig = harness.ToolCallHarnessConfig
    task.ToolCallHarnessError = harness.ToolCallHarnessError
    if arm == 'split_menu':
        policy = load_module(source / PACKAGE / 'workflows/research_reading_policy.py',
                             'novelty_agent_framework.workflows._experiment_menu_policy')
        task.ResearchReadingPolicy = policy.ResearchReadingPolicy
    builder = load_module(source / PACKAGE / 'tools/evidence_card_builder.py',
                          f'novelty_agent_framework.tools._experiment_builder_{arm}')
    return task, builder, source / PACKAGE / 'prompts'


async def prepare(root, fetch_fulltexts):
    fixture = root / 'fixture'
    fixture.mkdir(parents=True, exist_ok=False)
    runtime = next((BASE / PAPER / 'runtime').iterdir())
    requests = []
    for path in sorted((runtime / 'stages').glob('*run_research_task/input.json')):
        raw = read(path)
        if raw['current_task']['attempt'] != 1:
            continue
        request = TaskResearchRequest(subject_paper_id=PAPER, run_id='fixed-researcher-comparison-20261016',
            novelty_point=raw['current_point'], research_task=raw['current_task'],
            search_plan=raw['current_search_plan'], target_identity=raw['target_identity'])
        requests.append(request.model_dump(mode='json'))
    assert len(requests) == 2 and {r['novelty_point']['point_id'] for r in requests} == {'NP-1','NP-2'}
    write(fixture/'requests.json', requests)
    for arm, source in [('original',BASE),('split_menu',MENU)]:
        source = source / 'startup/source'
        for name in ('core/tool_call_harness.py','tools/evidence_card_builder.py','workflows/research_task.py',
                     'workflows/research_reading_policy.py'):
            path = source / PACKAGE / name
            if path.exists():
                target = fixture/'versions'/arm/PACKAGE/name
                target.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(path,target)
        for name in ('prompts','skills'):
            shutil.copytree(source/PACKAGE/name,fixture/'versions'/arm/PACKAGE/name)
    corpus = fixture/'corpus'
    for namespace in ('references','subject_references'):
        shutil.copytree(BASE/PAPER/namespace,corpus/PAPER/namespace)
    discoveries = {}
    for path in sorted((runtime/'tools').glob('*database_search.json')):
        record = read(path)
        if record['scope'].get('attempt') == 1 and not record['resolved_arguments'].get('full_text_source_record_ids'):
            discoveries.setdefault(record['scope']['point_id'],record['raw_result']['payload']['research_bundle'])
    assert set(discoveries) == {'NP-1','NP-2'}
    write(fixture/'discoveries.json',discoveries)
    # Freeze exactly one acquisition outcome for every discovered paper before any arm runs.
    if fetch_fulltexts:
        config = load_application_config(profile_path=ROOT/'docs/experiments/20261016_guided_reading/profile.json',environ={})
        workflow = build_workflow(config,output_root=corpus)
        db = workflow.services.task_researcher.tools.get('database_search')
        for raw in requests:
            request = TaskResearchRequest.model_validate(raw)
            ids = [r['source_record_id'] for r in discoveries[request.novelty_point.point_id]['source_records']]
            for start in range(0,len(ids),4):
                observation = await db.ainvoke(DatabaseSearchArguments(source_id='arxiv',
                    full_text_source_record_ids=ids[start:start+4]),scope=request)
                write(fixture/f'acquisition-{request.novelty_point.point_id}-{start}.json',observation.model_dump(mode='json'))
                print('captured full texts',request.novelty_point.point_id,start,flush=True)
    write(fixture/'manifest.json',{
        'source_runs':{'original':str(BASE.relative_to(ROOT)),'split_menu':str(MENU.relative_to(ROOT))},
        'fulltext_preparation':'one live acquisition per candidate batch' if fetch_fulltexts else 'previously captured bodies only',
        'files':{str(p.relative_to(fixture)):digest(p) for p in sorted(fixture.rglob('*')) if p.is_file()},
        'fixed_task_sha256':[canonical(r) for r in requests],
    })


def verify_fixture(root):
    fixture=root/'fixture'
    for name,sha in read(fixture/'manifest.json')['files'].items():
        if digest(fixture/name) != sha: raise ValueError(f'fixture checksum mismatch: {name}')


class FrozenRetrieval:
    source_id='arxiv'
    def __init__(self,store,discoveries,execution_log=None):
        self.reference_store,self.discoveries=store,discoveries
        self.execution_log=execution_log
        self.calls=[]
    def record(self,request,kind,record_ids=()):
        self.calls.append({'kind':kind,'point_id':request.novelty_point.point_id,
            'search_plan':request.search_plan.model_dump(mode='json'),
            'source_record_ids':list(record_ids),
            'fixture_note':'Replay fixed corpus; plan recorded but no provider queries executed.'})
        if self.execution_log is not None: write(self.execution_log,self.calls)
    async def ainvoke(self,request):
        self.record(request,'discovery')
        return ResearchBundle.model_validate(self.discoveries[request.novelty_point.point_id])
    async def acquire_full_texts(self,request,record_ids):
        self.record(request,'acquisition',record_ids)
        manifest=self.reference_store.load_manifest(request.subject_paper_id)
        known={r.source_record_id:r for r in manifest.source_records}
        if any(i not in known for i in record_ids): raise ValueError('unknown frozen source record')
        records=[known[i] for i in record_ids]
        works={r.work_id for r in records}
        artifacts=[a for a in manifest.artifacts if a.source_record_id in record_ids]
        return ResearchBundle(bundle_id='frozen-acquisition-'+canonical(record_ids)[:16],producer='frozen:arxiv',
            works=[w for w in manifest.works if w.work_id in works],source_records=records,artifacts=artifacts,
            warnings=['Acquisition outcome replayed from the fixed corpus; no provider request was made.'])


class FrozenDatabaseSearch(DatabaseSearchTool):
    def project_model_context(self,observation):
        result=super().project_model_context(observation)
        result['fixture_note']='Fixed experiment corpus; retrieval/acquisition are replayed, not new network requests.'
        return result


def metrics(runtime,trace,result,validation,builder_calls,elapsed):
    models=[read(p) for p in sorted((runtime/'llm_calls').glob('*.json'))]
    calls=[read(p) for p in sorted((runtime/'tools').glob('*.json'))]
    attempts=0
    for m in models:
        response=m.get('response') or {}
        if response.get('tool_calls'): attempts+=1
        else:
            try: value=json.loads(response.get('content') or '')
            except ValueError: continue
            if isinstance(value,dict) and 'option_id' in value: attempts+=1
    policy_rejections=sum(e.kind=='error' and ('action policy rejected selection' in (e.detail or '')
                         or 'reader required after database_search' in (e.detail or '')) for e in trace)
    executed=[c for c in calls if c.get('failure_phase')!='PRE_TOOL']
    tool_failures=sum(c['execution_status']!='SUCCESS' for c in executed)
    reads=Counter((r.namespace.value,r.artifact_id,r.char_start,r.char_end) for r in result.read_results)
    # TaskResearchResult de-duplicates read IDs, so use real tool observations for I/O repetition.
    actual_reads=[]
    for c in calls:
        if c['tool_name']!='reader':continue
        payload=(c.get('raw_result') or {}).get('payload',{})
        actual_reads.extend(payload.get('read_results') or ([payload['read_result']] if payload.get('read_result') else []))
    ranges=Counter((r['namespace'],r['artifact_id'],r['char_start'],r['char_end']) for r in actual_reads)
    generated=len(result.evidence_cards);accepted=len(validation.accepted)
    model_failures=sum(m['status']!='SUCCESS' for m in models)
    tokens=Counter()
    for m in models:
        for key,value in (m.get('tokens') or {}).items():
            if isinstance(value,(int,float)) and not isinstance(value,bool):tokens[key]+=value
    return {'task_status':result.status.value,'duration_seconds':round(elapsed,3),
        'model_calls':len(models),'model_failures':model_failures,
        'model_error_rate':model_failures/len(models) if models else None,
        'action_attempts':attempts,'policy_rejections':policy_rejections,
        'tool_calls':len(executed),'tool_failures':tool_failures,
        'pre_tool_stops':sum(c.get('failure_phase')=='PRE_TOOL' for c in calls),
        'tool_error_rate':tool_failures/len(executed) if executed else None,
        'action_error_rate':(policy_rejections+tool_failures)/attempts if attempts else None,
        'reader_requests':sum(c['tool_name']=='reader' for c in calls),
        'reader_calls':sum(c['tool_name']=='reader' for c in executed),
        'unique_read_ranges':len(ranges),'duplicate_read_ranges':sum(n-1 for n in ranges.values()),
        'read_characters':sum(r['char_end']-r['char_start'] for r in actual_reads),
        'generated_cards':generated,'accepted_cards':accepted,'rejected_cards':len(validation.rejected),
        'validator_pass_rate':accepted/generated if generated else None,
        'builder_invocations':len(builder_calls),'builder_rejection_count':sum(len(c['result']['rejections']) for c in builder_calls),
        'tokens':dict(tokens),'warnings':result.warnings,
        'model_errors':[m.get('error') for m in models if m['status']!='SUCCESS']}


async def run_case(root,arm,raw_request,*,decision_schema=False):
    if decision_schema and arm != 'choice_only':
        raise ValueError('decision schema experiment requires choice_only arm')
    request=TaskResearchRequest.model_validate(raw_request)
    case=root/'runs'/arm/request.novelty_point.point_id
    case.mkdir(parents=True,exist_ok=False)
    shutil.copytree(root/'fixture/corpus'/PAPER,case/PAPER)
    config=load_application_config(profile_path=ROOT/'docs/experiments/20261016_guided_reading/profile.json',environ={})
    config.researcher.harness.guided_reading=arm!='original'
    config.researcher.harness.text_choices=arm=='choice_only'
    config.project.runtime_debug.max_model_calls=40
    if arm=='choice_only':config.researcher.prompt='research/choice_only'
    frozen=prepare_startup(config,entrypoint='fixed_researcher_comparison',output_root=case,
        snapshot_dir=case/'startup',input_contents={'task-request.json':request.model_dump_json().encode()},
        active_roles=('researcher',),require_reviewer=False)
    standard=build_workflow(config,output_root=case).services.task_researcher
    store=ReferenceStore(case)
    db=FrozenDatabaseSearch({'arxiv':FrozenRetrieval(store,read(root/'fixture/discoveries.json'),case/'retrieval-calls.json')},reference_store=store)
    from novelty_agent_framework.tools import ResearcherToolRegistry
    registry=ResearcherToolRegistry([db if name=='database_search' else standard.tools.get(name) for name in standard.tools.names])
    task_module,builder_module,prompt_root=variant(root,arm)
    cfg=asdict(standard.config)
    cfg['model_options']=standard.config.model_options
    cfg={k:v for k,v in cfg.items() if k in task_module.TaskResearcherConfig.__dataclass_fields__}
    builder=builder_module.EvidenceCardBuilder(store)
    builder_calls=[]
    original_build=builder.build
    def measured_build(draft,**kwargs):
        built=original_build(draft,**kwargs)
        builder_calls.append({'draft':draft.model_dump(mode='json'),'result':built.model_dump(mode='json')})
        return built
    builder.build=measured_build
    researcher=task_module.TaskResearcherWorkflow(standard.model_client,registry,builder,
        prompts=PromptLibrary(prompt_root),config=task_module.TaskResearcherConfig(**cfg))
    # Choice mode now always uses the formal Harness decision schema.
    decision_schema = arm == "choice_only"
    # Freeze the ACTUAL historical variant's source/prompt identities separately from the host code.
    system,user=researcher._render_prompt(request)
    write(case/'actual-input.json',{'request':raw_request,'system':system,'user':user,
        'arm':arm,'decision_schema':decision_schema,'task_sha256':canonical(raw_request),'budgets':{k:v for k,v in cfg.items() if k!='model_options'},
        'variant_sources':{str(p.relative_to(root/'fixture')):digest(p)
            for p in (root/'fixture/versions'/arm).rglob('*.py')} if arm!='choice_only' else 'startup source snapshot'})
    captured=[]
    original_run=researcher.harness.run
    async def capture(**kwargs):
        try:
            result=await original_run(**kwargs);captured.extend(result.trace);return result
        except Exception as exc:
            captured.extend(getattr(exc,'trace',()));raise
    researcher.harness.run=capture
    manager=RuntimeArtifactManager(PAPER,run_id=f'comparison-{arm}-{request.novelty_point.point_id}',
        config=RuntimeDebugConfig(output_root=case,archive_root=root/'archive',max_model_calls=40),
        runtime_config={'arm':arm,'fixed_task_sha256':canonical(raw_request),'effective_config':frozen['effective_config'],
                        'frozen_provider':True,'decision_schema':decision_schema},stage_names=['run_research_task','validate_evidence'])
    manager.activate();started=time.perf_counter()
    try:
        stage=manager.start_stage('run_research_task',request)
        result=await researcher.ainvoke(request)
        manager.finish_stage(stage,result)
        stage=manager.start_stage('validate_evidence',{'cards':result.evidence_cards})
        validation=DefaultEvidenceValidator().validate(result.evidence_cards,tasks=[request.research_task],
            evidence=result.evidence,novelty_points=[request.novelty_point],paper_id=PAPER,reference_store=store)
        manager.finish_stage(stage,validation.integrity)
        write(case/'result.json',result.model_dump(mode='json'))
        write(case/'validator.json',{'integrity':validation.integrity,'rejected':validation.rejected})
        write(case/'builder-calls.json',builder_calls)
        write(case/'trace.json',[asdict(e) for e in captured])
        summary=metrics(manager.run_dir,captured,result,validation,builder_calls,time.perf_counter()-started)
        write(case/'metrics.json',summary)
        manager.record_outcome(summary);manager.finish_run('SUCCESS')
        print(arm,request.novelty_point.point_id,json.dumps({k:summary[k] for k in ('model_calls','generated_cards','accepted_cards','policy_rejections','duplicate_read_ranges')},ensure_ascii=False),flush=True)
    except BaseException as exc:
        manager.finish_run('FAILED',error=exc);raise
    finally:manager.deactivate()


def summarize(root):
    # Recompute from raw observations so reporting changes never require model reruns.
    for path in sorted((root/'runs').glob('*/*/metrics.json')):
        case=path.parent
        result=TaskResearchResult.model_validate(read(case/'result.json'))
        audit=read(case/'validator.json')
        validation=SimpleNamespace(accepted=audit['integrity']['accepted_card_ids'],rejected=audit['rejected'])
        trace=[SimpleNamespace(**e) for e in read(case/'trace.json')]
        runtime=next((case/PAPER/'runtime').iterdir())
        write(path,metrics(runtime,trace,result,validation,read(case/'builder-calls.json'),read(path)['duration_seconds']))
    summary={}
    for arm in ARMS:
        cases=[read(p) for p in sorted((root/'runs'/arm).glob('*/metrics.json'))]
        if not cases:continue
        sums={k:sum(c[k] for c in cases) for k in ('model_calls','model_failures','tool_calls','tool_failures','action_attempts',
            'policy_rejections','generated_cards','accepted_cards','rejected_cards','reader_calls','unique_read_ranges',
            'duplicate_read_ranges','read_characters','duration_seconds','builder_rejection_count')}
        sums.update({'tasks':len(cases),'task_statuses':dict(Counter(c['task_status'] for c in cases)),
            'tasks_with_valid_cards':sum(c['accepted_cards']>0 for c in cases),
            'validator_pass_rate':sums['accepted_cards']/sums['generated_cards'] if sums['generated_cards'] else None,
            'model_error_rate':sums['model_failures']/sums['model_calls'] if sums['model_calls'] else None,
            'tool_error_rate':sums['tool_failures']/sums['tool_calls'] if sums['tool_calls'] else None,
            'action_error_rate':(sums['policy_rejections']+sums['tool_failures'])/sums['action_attempts'] if sums['action_attempts'] else None,
            'tokens':dict(sum((Counter(c['tokens']) for c in cases),Counter()))})
        summary[arm]=sums
    write(root/'comparison.json',summary)
    print(json.dumps(summary,ensure_ascii=False,indent=2))


async def main(args):
    _load_dev_env()
    root=args.output.resolve()
    if args.prepare:
        await prepare(root,args.fetch_fulltexts);return
    verify_fixture(root)
    if args.summarize:summarize(root);return
    if args.decision_schema and args.arms != ['choice_only']:
        raise ValueError('--decision-schema requires --arms choice_only')
    requests=read(root/'fixture/requests.json')
    if args.points:
        unknown=set(args.points)-{r['novelty_point']['point_id'] for r in requests}
        if unknown: raise ValueError(f'unknown fixed points: {sorted(unknown)}')
        requests=[r for r in requests if r['novelty_point']['point_id'] in args.points]
    for arm in args.arms:
        for request in requests:
            await run_case(root,arm,request,decision_schema=args.decision_schema)
    summarize(root)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,default=DEFAULT)
    parser.add_argument('--prepare',action='store_true')
    parser.add_argument('--fetch-fulltexts',action='store_true')
    parser.add_argument('--summarize',action='store_true')
    parser.add_argument('--decision-schema',action='store_true',help='Compatibility flag: choice_only now always uses the formal Harness JSON Schema')
    parser.add_argument('--points',nargs='+',help='Run only selected fixed novelty point IDs')
    parser.add_argument('--arms',nargs='+',choices=ARMS,default=list(ARMS))
    asyncio.run(main(parser.parse_args()))
