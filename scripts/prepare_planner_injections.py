"""Freeze eligible generated drafts before independent controlled-review runs."""
import argparse
import hashlib
import json
from pathlib import Path
import re

from inspect_planner_acceptance import inspect
from run_planner_acceptance import output_root, validate_frozen, write


def choose_fixture(rows, kind):
    sample = 'gsaeru' if kind == 'omit' else 'museformer'
    for row in sorted(rows, key=lambda r: r['case_id']):
        if row['kind'] != 'natural' or row['sample'] != sample or row['scenario'] != 'A' or 'generated_draft' not in row:
            continue
        draft = row['generated_draft']
        concepts = draft['concepts']
        if draft.get('comparison') or any(len(c['terms']) != 1 for c in concepts):
            continue
        if kind == 'omit':
            candidates = [c for c in concepts if re.search(r'图摘要|图总结|graph summar', c['terms'][0], re.I)]
            if len(candidates) != 1 or len(concepts) < 2:
                continue
            target = candidates[0]
            if any(re.search(r'图摘要|图总结|graph summar', ' '.join(c['terms']+c.get('alias', [])), re.I)
                   for c in concepts if c is not target):
                continue
            return draft, dict(target=target['terms'][0]), row['case_id']
        fine = [c for c in concepts if '细粒度' in c['terms'][0] and '注意力' in c['terms'][0] and '粗粒度' not in c['terms'][0]]
        coarse = [c for c in concepts if '粗粒度' in c['terms'][0] and '注意力' in c['terms'][0] and '细粒度' not in c['terms'][0]]
        if len(fine) != 1 or len(coarse) != 1:
            continue
        aliases = sorted(a for a in fine[0].get('alias', []) if re.search(r'(细粒度.*注意力|fine.grained attention)', a, re.I)
                         and '粗粒度' not in a)
        if aliases:
            return draft, dict(target=fine[0]['terms'][0], alias=aliases[0], destination=coarse[0]['terms'][0]), row['case_id']
    raise ValueError(f'No eligible {kind} draft; do not count an injection whose conditions are absent')


def jobs_for_fixture(kind, spec, source_case, relative_fixture):
    return [dict(case_id=f'control-{i+(0 if kind=="omit" else 3):02}-zh',
                 sample='gsaeru' if kind == 'omit' else 'museformer', scenario='A', language='zh',
                 repeat=i, kind=kind, generation_fixture=relative_fixture,
                 source_case=source_case, **spec) for i in range(1, 4)]


def prepare(root, alias_generated_fixture=None):
    manifest = json.loads((root/'manifest.json').read_text())
    validate_frozen(manifest)
    if manifest['injection_jobs']:
        raise ValueError('Injection jobs already frozen')
    rows = inspect(root)
    if any(not r['audit_verified'] for r in rows):
        raise ValueError('Finish the natural matrix before selecting frozen injection fixtures')
    # Select both before writing; an absent condition cannot become a valid experiment.
    selected = {'omit': choose_fixture(rows, 'omit')}
    try:
        selected['alias'] = choose_fixture(rows, 'alias')
    except ValueError:
        if alias_generated_fixture is None:
            raise
        record = json.loads(alias_generated_fixture.read_text())
        from novelty_agent_framework.core.concept_stage import ConceptBatch
        generated = ConceptBatch.model_validate_json(record['response']).model_dump()
        if generated != record['draft'] or record['sample'] != 'museformer' or record['point_id'] != 'NP-1':
            raise ValueError('Control draft must match its preserved original generation response')
        selected['alias'] = choose_fixture([dict(case_id='prepared-control-source', kind='natural',
            sample='museformer', scenario='A', generated_draft=generated)], 'alias')
        manifest['injection_fixture_protocol_amendment'] = (
            'Natural drafts lacked an eligible alias. Use a separately model-generated, source-checked '
            'positive control draft; it is not a natural sample. Preserve the unmodified response and '
            'move the existing valid alias only before the first live review. Natural inputs and results are unchanged.')
    for kind, (draft, spec, source_case) in selected.items():
        relative = f'injection-fixtures/{kind}.json'
        write(root/relative, draft)
        manifest['injection_jobs'].extend(jobs_for_fixture(kind, spec, source_case, relative))
    manifest['injection_preparer_sha256'] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    manifest['injection_fixtures_sha256'] = {k: hashlib.sha256((root/f'injection-fixtures/{k}.json').read_bytes()).hexdigest() for k in selected}
    write(root/'manifest.json', manifest)
    print('Frozen 6 blinded injections with generated drafts; generation replay is explicitly recorded.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=output_root, required=True)
    parser.add_argument('--alias-generated-fixture', type=Path)
    args = parser.parse_args()
    prepare(args.root, args.alias_generated_fixture)
