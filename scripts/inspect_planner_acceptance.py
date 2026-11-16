"""Verify raw acceptance audits and project only evidence needed for assessment."""
import argparse
import json
from pathlib import Path

from novelty_agent_framework.core.concept_audit import read_attempt
from novelty_agent_framework.core.concept_review_v2 import EvidenceReview, validate_evidence_review


def inspect_case(root, job):
    folder = root/job['case_id']
    row = {**job}
    summary_paths = list(folder.glob('planner-attempts/*/planner/summary.json'))
    if not summary_paths:
        return {**row, 'execution': 'not_finished', 'audit_verified': False}
    directory = summary_paths[0].parent
    index = json.loads((directory/'index.json').read_text())
    files = read_attempt(directory, run_id=index['run_id'], point_id=index['point_id'], task_id=index['task_id'])
    summary = json.loads(files['summary.json'])
    concepts = summary.get('concept_result') or {}
    strategies = summary.get('strategy_result') or {}
    row.update(execution=summary['status'], audit_verified=True,
        calls={k: summary.get(k, 0) for k in ['semantic_calls', 'concept_calls', 'strategy_calls', 'replayed_generation_records']},
        metrics=summary.get('metrics', {}), accepted_concepts=concepts.get('accepted_concepts', {}),
        unresolved=concepts.get('unresolved', {}), fact_links=concepts.get('concept_fact_links', {}),
        accepted_strategies=strategies.get('accepted_strategies', []), failed_levels=strategies.get('failed_levels', {}),
        has_plan='plan.json' in files)
    if 'generated-draft.json' in files:
        row['generated_draft'] = json.loads(files['generated-draft.json'])
    if 'injection.json' in files:
        row['injection'] = json.loads(files['injection.json'])
    if 'semantic-contract.json' in files:
        row['contract'] = json.loads(files['semantic-contract.json'])
    if 'plan.json' in files:
        row['plan'] = json.loads(files['plan.json'])
        assert row['plan']['task_id'] == job['case_id']
    events = [json.loads(line) for line in files.get('events.jsonl', b'').decode().splitlines()]
    row['repairs'] = [e['payload'] for e in events if e['step'] == 'concept_issue_resolved']
    row['progress'] = [e['payload'] for e in events if e['step'] == 'concept_review_progress']
    row['initial_review'] = None
    for name in sorted(files):
        if not name.startswith('calls/'):
            continue
        call = json.loads(files[name])
        payload = call.get('input', {})
        if not payload.get('check_coverage') or not call.get('output'):
            continue
        try:
            value = EvidenceReview.model_validate_json(call['output'])
            validate_evidence_review(value, payload)
        except ValueError:
            continue
        row['initial_review'] = value.model_dump()
        break
    return row


def inspect(root):
    manifest = json.loads((root/'manifest.json').read_text())
    return [inspect_case(root, job) for job in manifest['jobs'] + manifest['injection_jobs']]


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    args = parser.parse_args()
    root = args.root.resolve()
    project = Path(__file__).resolve().parents[1]
    if not root.is_relative_to(project/'local-reports'):
        parser.error('Raw inspection must remain in local-reports')
    rows = inspect(root)
    (root/'inspection.json').write_text(json.dumps(rows, ensure_ascii=False, indent=2))
    print('Inspected cases:', len(rows), '; verified terminal audits:', sum(r['audit_verified'] for r in rows))
