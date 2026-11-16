"""Classify verified runs using independent, evidence-cited semantic assessments."""
import argparse
from collections import Counter
import json
from pathlib import Path

from inspect_planner_acceptance import inspect

DIMENSIONS = ('source_fidelity', 'terms_aliases', 'core_coverage', 'translation', 'strategy_quality', 'process_and_utility')


def classify(row, assessment):
    if not row.get('audit_verified'):
        return 'unverified'
    if row['execution'] in ('failed', 'cancelled', 'runner_failed') or not row.get('has_plan'):
        return 'not_met'
    if not assessment or assessment.get('basis') != 'independent_source_and_approved_rules':
        return 'unverified'
    dimensions = assessment.get('dimensions', {})
    if any(d.get('status') == 'fail' for d in dimensions.values()):
        return 'not_met'
    if set(dimensions) != set(DIMENSIONS) or any(
        d.get('status') not in ('pass', 'partial', 'not_applicable') or not d.get('reason') or not d.get('evidence')
        for d in dimensions.values()
    ):
        return 'unverified'
    levels = {s['level'] for s in row.get('accepted_strategies', [])}
    if levels == {'strict', 'medium', 'broad'} and not row.get('unresolved') and all(d['status'] != 'partial' for d in dimensions.values()):
        return 'complete'
    return 'expected_partial'


def summarize(root, assessments):
    rows = inspect(root)
    by_case = {x['case_id']: x for x in assessments}
    result = []
    for row in rows:
        assessment = by_case.get(row['case_id'])
        result.append(dict(case_id=row['case_id'], sample=row['sample'], scenario=row['scenario'],
            repeat=row['repeat'], language=row['language'], kind=row['kind'], execution=row['execution'],
            experiment_mode='natural' if row['kind']=='natural' else 'isolated_control' if row.get('semantic_fixture') else 'end_to_end_control',
            injection_applied='injection' in row,
            audit_verified=row['audit_verified'], acceptance=classify(row, assessment),
            calls=row.get('calls', {}), accepted_levels=[s['level'] for s in row.get('accepted_strategies', [])],
            unresolved_concepts=len(row.get('unresolved', {})), has_plan=row.get('has_plan', False),
            assessment=assessment))
    scenarios = {}
    for row in result:
        if row['kind'] == 'natural':
            scenarios.setdefault((row['sample'], row['scenario'], row['repeat']), []).append(row['acceptance'])
    scenario_counts = Counter()
    for states in scenarios.values():
        verdict = ('not_met' if 'not_met' in states else 'unverified' if 'unverified' in states else
                   'expected_partial' if 'expected_partial' in states else 'complete')
        scenario_counts[verdict] += 1
    return dict(schema_version=1, natural_tasks=30, natural_scenarios=24,
        task_counts=dict(Counter(r['acceptance'] for r in result if r['kind']=='natural')),
        scenario_counts=dict(scenario_counts),
        execution_counts=dict(Counter(r['execution'] for r in result if r['kind']=='natural')),
        injection_tasks=sum(r['kind'] != 'natural' for r in result), cases=result)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--assessments', type=Path, required=True)
    args = parser.parse_args()
    root = args.root.resolve()
    if not root.is_relative_to(Path(__file__).resolve().parents[1]/'local-reports'):
        parser.error('Raw aggregation must remain in local-reports')
    value = summarize(root, json.loads(args.assessments.read_text()))
    (root/'acceptance-summary.json').write_text(json.dumps(value, ensure_ascii=False, indent=2))
    print(json.dumps({k:v for k,v in value.items() if k!='cases'}, ensure_ascii=False))
