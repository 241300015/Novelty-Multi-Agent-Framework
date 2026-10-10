import argparse,json
from pathlib import Path
from datetime import datetime
p=argparse.ArgumentParser();p.add_argument('audit',type=Path);a=p.parse_args()
d=json.loads(a.audit.read_text())
workspace=Path(d['runs_root'])/d['task_id']/'MF2033k6lC'
events=[];stages=[]
for path in workspace.glob('runtime/*/stages/*/meta.json'):
 m=json.loads(path.read_text());stages.append(m)
 if m.get('stage_name')=='run_research_task' and m.get('finished_at'):
  events.extend([(datetime.fromisoformat(m['started_at']),1),(datetime.fromisoformat(m['finished_at']),-1)])
active=0;peak=0
for timestamp,change in sorted(events,key=lambda x:(x[0],x[1])):
 active+=change;peak=max(peak,active)
d['observed_research_concurrency']=peak
d['stage_statuses']={name:sorted({m['status'] for m in stages if m['stage_name']==name}) for name in sorted({m['stage_name'] for m in stages})}
for path in workspace.glob('runtime/*/stages/*validate_report_integrity/output.json'):
 out=json.loads(path.read_text());d['report_integrity']=out.get('report_integrity',{})
d['all_calls_use_private_cloud']=all(json.loads(path.read_text()).get('alias')=='deepseek-official-flash' for path in workspace.glob('runtime/*/llm_calls/*.json'))
if d['passed']:
 d['passed']=peak>1 and d['all_calls_use_private_cloud'] and d.get('report_integrity',{}).get('validation_passed') is True
 d['acceptance_scope']='Current API PaperInput execution, real cloud calls, concurrent research, existing artifact persistence, integrity and report download; PDF parsing and browser UI excluded.'
a.audit.write_text(json.dumps(d,ensure_ascii=False,indent=2)+'\n')
print(json.dumps({k:d.get(k) for k in ['status','point_count','card_count','rounds','model_call_count','observed_research_concurrency','report_integrity','passed']},ensure_ascii=False))
raise SystemExit(0 if d['passed'] else 1)
