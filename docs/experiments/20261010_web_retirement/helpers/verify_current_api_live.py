import argparse, concurrent.futures, hashlib, json, os, subprocess, time
from pathlib import Path
from fastapi.testclient import TestClient
from novelty_agent_framework.config import NoveltyWebSettings, load_application_config
from novelty_agent_framework.main import create_app
from novelty_agent_framework.services import build_real_workflow_service

p=argparse.ArgumentParser()
p.add_argument('--paper-json',type=Path,required=True)
p.add_argument('--runs-root',type=Path,required=True)
p.add_argument('--audit',type=Path,required=True)
p.add_argument('--model-alias',default='deepseek-flash')
a=p.parse_args()
a.runs_root.mkdir(parents=True,exist_ok=True)
a.audit.parent.mkdir(parents=True,exist_ok=True)
config=load_application_config()
roles=['coordinator','point_extractor','search_planner','researcher','reviewer']
aliases={r:getattr(config,r).model.alias for r in roles}
assert set(aliases.values())=={a.model_alias}, aliases
assert config.project.workflow.max_concurrency==4
settings=NoveltyWebSettings(workflow_mode='real',runs_root=a.runs_root)
service=build_real_workflow_service(settings)
app=create_app(settings,service=service,static_root=Path('/tmp/no-static-web-retirement'))
paper=json.loads(a.paper_json.read_text())
summary={'commit':subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip(),'input':str(a.paper_json),'input_sha256':hashlib.sha256(a.paper_json.read_bytes()).hexdigest(),'model_aliases':aliases,'max_concurrency':config.project.workflow.max_concurrency,'runs_root':str(a.runs_root),'pdf_parser_tested':False}
start=time.monotonic()
with TestClient(app) as client:
 summary['health']=client.get('/api/novelty/health').json()
 summary['retired_routes_status']={path:client.get(path).status_code for path in ['/api/health','/api/runs/unknown']}
 assert not hasattr(app.state,'pdf_runs')
 assert summary['health']['status']=='ready'
 assert set(summary['retired_routes_status'].values())=={404}
 print(json.dumps({'event':'ready',**summary},ensure_ascii=False),flush=True)
 with concurrent.futures.ThreadPoolExecutor(max_workers=1) as request_worker:
  future=request_worker.submit(client.post,'/api/novelty/runs',json=paper)
  last=None
  last_print=0
  while not future.done():
   with service.store._lock:
    snapshots=list(service.store._runs.values())
   state=[(s.task_id,str(s.status),s.progress.model_dump() if s.progress else None) for s in snapshots]
   if state!=last or time.monotonic()-last_print>=30:
    print(json.dumps({'event':'progress','elapsed_seconds':round(time.monotonic()-start),'tasks':state},ensure_ascii=False),flush=True)
    last=state;last_print=time.monotonic()
   time.sleep(2)
  response=future.result()
 summary['create_status']=response.status_code
 task_id=response.json()['task_id']
 summary['task_id']=task_id
 snapshot=client.get(f'/api/novelty/runs/{task_id}').json()
 summary['status']=snapshot['status']
 summary['error']=snapshot.get('error')
 result=snapshot.get('result') or {}
 summary['point_count']=len((result.get('brief') or {}).get('novelty_points',[]))
 summary['card_count']=len(result.get('evidence_cards',[]))
 summary['rounds']=result.get('rounds')
 summary['reviews']=[{k:r.get(k) for k in ['novelty_point_id','status','incomplete_reason','verdict']} for r in result.get('novelty_reviews',[])]
 summary['issue_codes']=sorted({i['code'] for i in result.get('issues',[])})
 report=snapshot.get('report')
 if report:
  preview=client.get(report['preview_url'])
  download=client.get(report['downloads']['md'])
  summary['report_http_status']=preview.status_code
  summary['report_bytes']=len(preview.content)
  summary['preview_download_identical']=preview.content==download.content
  summary['report_sha256']=hashlib.sha256(preview.content).hexdigest()
 workspace=a.runs_root/task_id
 summary['persisted_artifacts']={name:int((workspace/name if name=='paper-input.json' else workspace/paper['paper_id']/name).is_file()) for name in ['paper-input.json','novelty-points.json','retrieval-plans.json','evidence-cards.json','novelty-reviews.json','report.json']}
 logs=list((workspace/paper['paper_id']).glob('runtime/*/llm_calls/*.json'))
 calls=[json.loads(path.read_text()) for path in logs]
 summary['model_call_count']=len(calls)
 summary['elapsed_seconds']=round(time.monotonic()-start,2)
 summary['passed']=(summary['create_status']==202 and summary['status']=='succeeded' and summary['point_count']>0 and summary['card_count']>0 and len(summary['reviews'])==summary['point_count'] and all(r.get('incomplete_reason') not in {'technical_error','budget_exhausted'} for r in summary['reviews']) and summary.get('report_http_status')==200 and summary.get('preview_download_identical') is True and all(summary['persisted_artifacts'].values()))
a.audit.write_text(json.dumps(summary,ensure_ascii=False,indent=2)+'\n')
print(json.dumps({'event':'finished',**summary},ensure_ascii=False),flush=True)
raise SystemExit(0 if summary['passed'] else 1)
