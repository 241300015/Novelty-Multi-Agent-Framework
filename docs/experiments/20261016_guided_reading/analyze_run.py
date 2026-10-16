"""Summarize the isolated run without copying full prompts or provider payloads."""
import json
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
RUN = ROOT / 'outputs/guided-reading-20261016/runs/0001'
PAPER = RUN / 'MF2033k6lC'
RUNTIME = next((PAPER / 'runtime').iterdir())

def read(path):
    return json.loads(path.read_text())

def rows(pattern):
    return [read(path) for path in sorted(RUNTIME.glob(pattern))]

models = rows('llm_calls/*.json')
tools = rows('tools/*.json')
stages = rows('stages/*/meta.json')
results = [read(p) for p in sorted(PAPER.glob('research-runs/*/*/attempt-*.json'))]
read_calls = [t for t in tools if t['tool_name'] == 'reader']
reads = []
for t in read_calls:
    raw = (t.get('raw_result') or {}).get('payload', {})
    for r in raw.get('read_results') or [raw.get('read_result', {})]:
        if r:
            reads.append({'scope': t['scope'], 'artifact_id': r['artifact_id'],
                          'char_start': r['char_start'], 'char_end': r['char_end'],
                          'role': r['role'], 'has_more': r['has_more']})
keys = Counter((json.dumps(r['scope'], sort_keys=True),r['artifact_id'],r['char_start'],r['char_end'])
               for r in reads)
selection_rejections = []
for m in models:
    if m.get('stage_name') != 'run_research_task':
        continue
    definitions = {t.get('function',{}).get('name'): t.get('function',{})
                   for t in m.get('request_payload',{}).get('tools',[])}
    for call in (m.get('response') or {}).get('tool_calls',[]):
        name = call['name']
        args = call.get('arguments',{})
        if name != 'choose_research_action':
            selection_rejections.append({'llm_call_id':m['llm_call_id'], 'scope':m['scope'], 'reason':'raw_tool_name'})
        elif args.get('option_id') not in definitions.get(name,{}).get('parameters',{}).get('properties',{}).get('option_id',{}).get('enum',[]):
            selection_rejections.append({'llm_call_id':m['llm_call_id'], 'scope':m['scope'], 'reason':'stale_or_unknown_option'})
summary = {
    'run': read(RUN/'run.json'), 'runtime_run_id': RUNTIME.name,
    'stage_statuses': dict(Counter(s.get('status') for s in stages)),
    'model_statuses': dict(Counter(m['status'] for m in models)),
    'model_calls_by_stage': dict(Counter(m.get('stage_name') for m in models)),
    'tool_statuses': dict(Counter(t['execution_status'] for t in tools)),
    'tools_by_name': dict(Counter(t['tool_name'] for t in tools)),
    'successful_reads': reads, 'duplicate_read_ranges_within_task_attempt': sum(n-1 for n in keys.values()),
    'rejected_model_selections': selection_rejections,
    'research_tasks': [{'point_id':r['novelty_point_id'],'task_id':r['task_id'],'status':r['status'],
                        'read_count':len(r['read_results']), 'card_count':len(r['evidence_cards']),
                        'warnings':r['warnings']} for r in results],
    'total_generated_cards': sum(len(r['evidence_cards']) for r in results),
    'model_errors': [{'id':m['llm_call_id'],'scope':m['scope'],'error':m.get('error')}
                     for m in models if m['status'] != 'SUCCESS'],
    'report_path': str((PAPER/'report/MF2033k6lC-report.md').relative_to(ROOT)),
    'limitations': ['The run regenerated 4 novelty points; the earlier baseline had 2. This is not a controlled quality comparison.',
                    'The final capability-note cleanup was made after this run started; it was covered by targeted offline tests, not this live snapshot.',
                    'A successful workflow exit with zero Cards is not evidence-path acceptance.'],
}
Path(__file__).with_name('analysis.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2)+'\n')
print(json.dumps({k:v for k,v in summary.items() if k in {'stage_statuses','model_statuses','tools_by_name','tool_statuses','total_generated_cards','duplicate_read_ranges_within_task_attempt'}},ensure_ascii=False,indent=2))
print('rejected model selections:',len(selection_rejections))
