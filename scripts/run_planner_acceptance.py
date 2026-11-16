"""Fixed Planner acceptance matrix; raw records stay in ignored local-reports."""
import argparse
import asyncio
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import ipaddress
import json
from pathlib import Path
import subprocess
import sys
import time
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
BENCHMARK = ROOT / 'docs/experiments/planner-concept-stage/acceptance-20261116/benchmark.json'


def digest(data):
    return hashlib.sha256(data).hexdigest()


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str))
    temporary.replace(path)


def output_root(value):
    path = Path(value).resolve()
    if not path.is_relative_to(ROOT / 'local-reports'):
        raise ValueError('Raw acceptance output must be inside ignored local-reports')
    return path


def fingerprints():
    from novelty_agent_framework.core.concept_audit import implementation_digest
    from novelty_agent_framework.config.loader import load_application_config
    return dict(implementation=implementation_digest(), runner=digest(Path(__file__).read_bytes()),
                benchmark=digest(BENCHMARK.read_bytes()),
                config=digest(json.dumps(load_application_config().model_dump(mode='json'), sort_keys=True).encode()))


def matrix(benchmark):
    jobs = []
    for sample in benchmark['samples']:
        for scenario in 'ABCD':
            for repeat in range(1, 4):
                for language in ('zh', 'en') if scenario == 'D' else (('en',) if scenario == 'B' else ('zh',)):
                    jobs.append(dict(case_id=f"{sample['id']}-{scenario}-{repeat}-{language}",
                        sample=sample['id'], scenario=scenario, repeat=repeat, language=language, kind='natural'))
    return jobs


def task_point(sample, scenario):
    from novelty_agent_framework.schemas import NoveltyPoint
    data = json.loads((ROOT/sample['source']).read_text())
    value = next(p for p in data['novelty_points'] if p['point_id'] == sample['point_id'])
    point = NoveltyPoint.model_validate(value)
    if scenario in ('A', 'B'):
        point = point.model_copy(update={'claim_en': '', 'technical_features_en': []})
    elif scenario == 'C':
        point = point.model_copy(update={'claim': '', 'technical_features': []})
    return point, data['paper_id']


def inject(batch, spec):
    """Only edit the generated draft; never construct reviewer decisions/issues."""
    result = batch.model_copy(deep=True)
    targets = [i for i, c in enumerate(result.concepts) if spec['target'] in c.terms]
    if len(targets) != 1:
        raise ValueError('injection_condition_not_met: target must occur exactly once')
    index = targets[0]
    if spec['kind'] == 'omit':
        if len(result.concepts) < 2:
            raise ValueError('injection_condition_not_met: omission would empty the draft')
        del result.concepts[index]
    else:
        destinations = [i for i, c in enumerate(result.concepts) if spec['destination'] in c.terms]
        if len(destinations) != 1 or destinations[0] == index or spec['alias'] not in result.concepts[index].alias:
            raise ValueError('injection_condition_not_met: alias and distinct destination required')
        result.concepts[index].alias.remove(spec['alias'])
        result.concepts[destinations[0]].alias.append(spec['alias'])
    return result


def configured_agent():
    from backend.env import OpenAICompatibleChatClient
    from novelty_agent_framework.config.factory import build_model_registry, build_prompt_library, build_search_planner
    from novelty_agent_framework.config.loader import legacy_shape, load_application_config
    cfg = load_application_config()
    registry = build_model_registry(cfg)
    agent = build_search_planner(legacy_shape(cfg), registry, build_prompt_library())
    agent.concept_review = True
    agent.concept_review_protocol = 'v2'
    agent.use_semantic_contract = True
    agent.strategy_review = True
    profile = registry.client_for(agent._model_alias).profile
    host = urlparse(profile.base_url).hostname
    if host != 'localhost' and not ipaddress.ip_address(host).is_loopback:
        raise ValueError('Acceptance uses the configured loopback model service only')
    if not profile.api_key:
        raise ValueError('Configured model credential is missing; no acceptance jobs were started')
    agent.model_client = OpenAICompatibleChatClient(profile)
    return agent


def prepare(root):
    if (root/'manifest.json').exists():
        raise ValueError('Use the existing frozen manifest or a new output directory')
    benchmark = json.loads(BENCHMARK.read_text())
    configured_agent()
    for sample in benchmark['samples']:
        task_point(sample, 'D')
    manifest = dict(schema_version=1, commit=subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT).decode().strip(),
        fingerprints=fingerprints(), benchmark=benchmark, max_model_calls=32, case_timeout_seconds=600,
        source_hashes={s['id']: digest((ROOT/s['source']).read_bytes()) for s in benchmark['samples']},
        jobs=matrix(benchmark), injection_jobs=[], results=[])
    write(root/'manifest.json', manifest)
    print('Frozen 24 scenarios / 30 tasks, 32 actual model calls per task.', flush=True)


def validate_frozen(manifest):
    if manifest['fingerprints'] != fingerprints():
        raise ValueError('Frozen implementation, runner, benchmark or config changed')
    for sample in manifest['benchmark']['samples']:
        if digest((ROOT/sample['source']).read_bytes()) != manifest['source_hashes'][sample['id']]:
            raise ValueError('Frozen source changed')


async def case(root, case_id):
    from novelty_agent_framework.core.runtime_artifacts import RuntimeArtifactManager, RuntimeDebugConfig
    from novelty_agent_framework.schemas import ResearchTask
    manifest = json.loads((root/'manifest.json').read_text())
    validate_frozen(manifest)
    job = next(j for j in manifest['jobs'] + manifest['injection_jobs'] if j['case_id'] == case_id)
    sample = next(s for s in manifest['benchmark']['samples'] if s['id'] == job['sample'])
    point, paper_id = task_point(sample, job['scenario'])
    task = ResearchTask(task_id=case_id, novelty_point_id=point.point_id, language=job['language'], task_type='literature_search')
    agent = configured_agent()
    folder = root/case_id
    base_run = agent._run_plan
    base_generate = agent.generate_concepts

    async def instrumented_run(point, task, *, use_async_client, audit):
        async def generate(call, payload):
            if job['kind'] != 'natural':
                frozen = json.loads((root/job['generation_fixture']).read_text())
                async def replay(instruction, schema, actual_payload):
                    audit.event(0, 'concept_call', dict(operation=instruction, input=actual_payload,
                        output=json.dumps(frozen, ensure_ascii=False), replayed=True))
                    return schema.model_validate(frozen)
                batch = await base_generate(replay, payload)
            else:
                batch = await base_generate(call, payload)
            audit.write('generated-draft.json', batch.model_dump())
            if job['kind'] != 'natural':
                modified = inject(batch, job)
                audit.write('injection.json', dict(spec=job, before=batch.model_dump(), after=modified.model_dump()))
                return modified
            return batch
        agent.generate_concepts = generate
        return await base_run(point, task, use_async_client=use_async_client, audit=audit)

    agent._run_plan = instrumented_run
    manager = RuntimeArtifactManager(paper_id, config=RuntimeDebugConfig(output_root=folder,
        archive_root=folder/'runtime-archive', max_model_calls=manifest['max_model_calls']),
        runtime_config={'max_model_calls': manifest['max_model_calls']})
    started = time.monotonic()
    result = dict(case_id=case_id, status='running', kind=job['kind'], semantic_review='pending')
    with manager:
        try:
            plan = await asyncio.wait_for(agent.aplan(point, task), manifest['case_timeout_seconds'])
            write(folder/'plan.json', plan.model_dump(mode='json'))
            summary = json.loads(next(folder.glob('planner-attempts/*/planner/summary.json')).read_text())
            result.update(status=summary['status'])
        except Exception as exc:
            result.update(status='failed', error_type=type(exc).__name__, error=str(exc))
            manager.finish_run('FAILED', error=exc)
        else:
            manager.finish_run('SUCCESS')
    result['elapsed_seconds'] = round(time.monotonic()-started, 2)
    write(folder/'result.json', result)
    print(case_id, result['status'], flush=True)


def batch(root, phase, workers):
    manifest = json.loads((root/'manifest.json').read_text())
    validate_frozen(manifest)
    jobs = manifest['jobs'] if phase == 'natural' else manifest['injection_jobs']
    if not jobs:
        raise ValueError('No frozen jobs for this phase')
    def execute(job):
        folder = root/job['case_id']
        folder.mkdir(parents=True, exist_ok=True)
        if (folder/'result.json').exists():
            return json.loads((folder/'result.json').read_text())
        with (folder/'runner.log').open('w') as stream:
            completed = subprocess.run([sys.executable, str(Path(__file__).resolve()), '--output', str(root),
                '--case', job['case_id']], cwd=ROOT, stdout=stream, stderr=subprocess.STDOUT,
                timeout=manifest['case_timeout_seconds']+60)
        if (folder/'result.json').exists():
            return json.loads((folder/'result.json').read_text())
        return dict(case_id=job['case_id'], kind=job['kind'], status='runner_failed', exit_code=completed.returncode)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(execute, job): job for job in jobs}
        for future in as_completed(futures):
            job = futures[future]
            try:
                result = future.result()
            except Exception as exc:
                result = dict(case_id=job['case_id'], kind=job['kind'], status='runner_failed', error_type=type(exc).__name__)
            manifest['results'] = [r for r in manifest['results'] if r['case_id'] != result['case_id']] + [result]
            write(root/'manifest.json', manifest)
            print(result['case_id'], result['status'], flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=output_root, required=True)
    parser.add_argument('--phase', choices=('prepare', 'natural', 'injection'))
    parser.add_argument('--case')
    parser.add_argument('--workers', type=int, choices=(1, 2), default=2)
    args = parser.parse_args()
    if args.case:
        asyncio.run(case(args.output, args.case))
    elif args.phase == 'prepare':
        prepare(args.output)
    else:
        batch(args.output, args.phase, args.workers)
