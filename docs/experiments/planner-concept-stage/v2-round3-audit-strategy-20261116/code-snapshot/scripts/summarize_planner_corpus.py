"""Summarize persisted Planner outcomes by plan-v2 flowchart node."""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path


def summarize(root):
    batch = json.loads((root/'batch.json').read_text())
    cases = []
    for result in batch['results']:
        directory = Path(result['output'])
        planner = next((directory/'planner-attempts').iterdir())/'planner'
        summary = json.loads((planner/'summary.json').read_text())
        events = [json.loads(line) for line in (planner/'events.jsonl').read_text().splitlines()] if (planner/'events.jsonl').exists() else []
        calls = [e['payload'] for e in events if e['step'] == 'concept_call']
        last = calls[-1]['input'] if calls else {}
        if result['status'] in ('completed', 'partial'):
            node = 'S'
        elif 'candidates' not in last:
            node = 'D'
        elif last.get('check_coverage'):
            node = 'E/F'
        else:
            node = 'K/L'
        resolutions = [e['payload'] for e in events if e['step'] == 'concept_issue_resolved']
        runtime = next(directory.glob('*/runtime'))
        model_calls = sum(len(list((run/'llm_calls').glob('*.json'))) for run in runtime.iterdir())
        index = json.loads((planner/'index.json').read_text())
        verified = all(hashlib.sha256((planner/name).read_bytes()).hexdigest() == digest for name, digest in index['files'].items())
        plan = json.loads((directory/'plan.json').read_text()) if (directory/'plan.json').exists() else None
        resolved = [d for e in events if e['step'] == 'concept_evidence_resolved' for d in e['payload']['decisions']]
        cases.append(dict(paper_id=result['paper_id'], point_id=result['point_id'], status=result['status'],
            audit_status=summary['status'],
            flow_node=node, model_calls=model_calls, concept_calls=summary['concept_calls'],
            accepted=len(summary['concept_result'].get('accepted', [])),
            unresolved=len(summary['concept_result'].get('unresolved', {})),
            repair_reviews=len(resolutions), changed=sum(e['changed'] for e in resolutions),
            resolution_counts=dict(Counter(e['resolution'] for e in resolutions)),
            evidence_decisions=len(resolved), resolved_spans=sum(len(d['resolved_evidence']) for d in resolved),
            indexed_files=len(index['files']), hashes_valid=verified, error=result.get('error'),
            broad=next((s['description'] for s in plan['strategies'] if s['level']=='broad'), None) if plan else None,
            output=str(directory)))
    return dict(protocol=batch['protocol'], cases=sorted(cases, key=lambda r:(r['paper_id'],r['point_id'])),
        completed=sum(c['status'] in ('completed','partial') for c in cases),
        plans_produced=sum(c['status'] in ('completed','partial') for c in cases),
        failed=sum(c['status'] not in ('completed','partial') for c in cases),
        clean_completed=sum(c['audit_status']=='completed' for c in cases),
        partial_plans=sum(c['audit_status']=='partial' for c in cases),
        model_calls=sum(c['model_calls'] for c in cases), concept_calls=sum(c['concept_calls'] for c in cases),
        failure_nodes=dict(Counter(c['flow_node'] for c in cases if c['status'] not in ('completed','partial'))),
        all_hashes_valid=all(c['hashes_valid'] for c in cases))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('current', type=Path)
    parser.add_argument('--baseline', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    result = dict(current=summarize(args.current))
    if args.baseline:
        result['baseline'] = summarize(args.baseline)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print(json.dumps({key:{k:v for k,v in value.items() if k!='cases'} for key,value in result.items()}, ensure_ascii=False))
