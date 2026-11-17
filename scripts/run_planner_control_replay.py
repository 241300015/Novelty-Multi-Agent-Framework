"""Isolate concept recovery with source-first standards and generated drafts replayed."""
import argparse
import asyncio
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json
from pathlib import Path
import subprocess
import sys

import run_planner_acceptance as runner


def prepare(root):
    manifest = json.loads((root/'manifest.json').read_text())
    runner.validate_frozen(manifest)
    if any(j.get('semantic_fixture') for j in manifest['injection_jobs']):
        raise ValueError('Replay controls already frozen')
    if any(not (root/j['case_id']/'result.json').exists() for j in manifest['injection_jobs']):
        raise ValueError('Finish and preserve the original six end-to-end control attempts first')
    for sample, source in [('gsaeru', 'gsaeru-A-1-zh'), ('museformer', 'museformer-A-2-zh')]:
        directory = next((root/source).glob('planner-attempts/*/planner'))
        runner.write(root/f'semantic-fixtures/{sample}.json', dict(
            contract=json.loads((directory/'semantic-contract.json').read_text()),
            catalog=json.loads((directory/'sources.json').read_text()), source_case=source))
    for index, original in enumerate(list(manifest['injection_jobs']), 7):
        manifest['injection_jobs'].append({**original, 'case_id': f'control-{index:02}-zh',
            'semantic_fixture': f"semantic-fixtures/{original['sample']}.json"})
    manifest['control_replay_driver_sha256'] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    manifest['control_replay_method'] = (
        'Additional isolated controls replay standards generated from source before any candidates, '
        'and replay the normal generated draft. Evidence IDs are rebound mechanically to the new task. '
        'All post-injection detection, correction, independent review and strategy calls are live. '
        'These are not natural end-to-end successes and do not replace the six original attempts.')
    runner.write(root/'manifest.json', manifest)
    print('Frozen six additional isolated controls; prior failures preserved.')


async def run_case(root, case_id):
    from novelty_agent_framework.core.concept_evidence import build_catalog
    from novelty_agent_framework.core.task_semantics import (
        SemanticFacts, SemanticPolicy, TechnicalFact, validate_contract_binding, validate_facts, validate_policy,
    )
    manifest = json.loads((root/'manifest.json').read_text())
    if manifest['control_replay_driver_sha256'] != hashlib.sha256(Path(__file__).read_bytes()).hexdigest():
        raise ValueError('Frozen control replay driver changed')
    job = next(j for j in manifest['injection_jobs'] if j['case_id'] == case_id)
    fixture = json.loads((root/job['semantic_fixture']).read_text())
    factory = runner.configured_agent

    def configured():
        agent = factory()
        async def semantic_phase(point, task, use_async_client, record):
            expected = hashlib.sha256(json.dumps(point.model_dump(), sort_keys=True, ensure_ascii=False).encode()).hexdigest()
            if expected != fixture['contract']['input_sha256']:
                raise ValueError('Control standard does not belong to the same source input')
            original = fixture['contract']
            validate_contract_binding(original, point, task.model_copy(update={'task_id': original['task_id']}))
            catalog = build_catalog(point, task)
            old = fixture['catalog']['sources']
            new = catalog['sources']
            if len(old) != len(new) or any(
                tuple(a[k] for k in ('text','field','language')) != tuple(b[k] for k in ('text','field','language'))
                for a,b in zip(old,new)):
                raise ValueError('Control evidence catalog changed')
            ids = {a['source_id']: b['source_id'] for a, b in zip(old, new)}
            facts = {
                fid: {**fact, 'evidence_ids': [ids[key] for key in fact['evidence_ids']]}
                for fid, fact in original['facts'].items()
            }
            # Replay the admitted standard so sparse fact IDs and any confirmed
            # actions keep their original meaning without another model vote.
            extracted = SemanticFacts.model_validate(dict(research_goal=original['research_goal'], facts=[
                {key: fact[key] for key in TechnicalFact.model_fields if key in fact} for fact in facts.values()
            ]))
            validate_facts(extracted, point, task, catalog)
            validate_policy(SemanticPolicy.model_validate(original['levels']), facts)
            contract = {key: value for key, value in original.items() if key != 'contract_id'}
            contract.update(task_id=task.task_id, facts=facts)
            contract['contract_id'] = hashlib.sha256(
                json.dumps(contract, sort_keys=True, ensure_ascii=False).encode()
            ).hexdigest()
            record(0, 'concept_source_catalog', catalog)
            record(0, 'semantic_call', dict(operation='facts', output=extracted.model_dump_json(), replayed=True))
            record(0, 'semantic_call', dict(operation='policy', output=json.dumps(original['levels'], ensure_ascii=False), replayed=True))
            record(0, 'task_semantic_contract', contract)
            return contract
        agent._semantic_phase = semantic_phase
        return agent
    runner.configured_agent = configured
    await runner.case(root, case_id)


def batch(root):
    manifest = json.loads((root/'manifest.json').read_text())
    jobs = [j for j in manifest['injection_jobs'] if j.get('semantic_fixture')]
    def execute(job):
        folder = root/job['case_id']
        folder.mkdir(parents=True, exist_ok=True)
        if (folder/'result.json').exists():
            return json.loads((folder/'result.json').read_text())
        with (folder/'runner.log').open('w') as stream:
            subprocess.run([sys.executable, str(Path(__file__).resolve()), '--root', str(root), '--case', job['case_id']],
                cwd=runner.ROOT, stdout=stream, stderr=subprocess.STDOUT, timeout=660, check=True)
        return json.loads((folder/'result.json').read_text())
    with ThreadPoolExecutor(max_workers=2) as pool:
        for future in as_completed({pool.submit(execute,j):j for j in jobs}):
            result = future.result()
            manifest['results'] = [r for r in manifest['results'] if r['case_id'] != result['case_id']] + [result]
            runner.write(root/'manifest.json', manifest)
            print(result['case_id'], result['status'], flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=runner.output_root, required=True)
    parser.add_argument('--prepare', action='store_true')
    parser.add_argument('--case')
    args = parser.parse_args()
    if args.prepare:
        prepare(args.root)
    elif args.case:
        asyncio.run(run_case(args.root,args.case))
    else:
        batch(args.root)
