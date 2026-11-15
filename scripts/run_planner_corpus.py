"""Run full Planner on each corpus point, with isolated per-case processes."""
import argparse
import asyncio
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time
from urllib.parse import urlparse


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str))
    temporary.replace(path)


async def case(args):
    from backend.env import OpenAICompatibleChatClient
    from novelty_agent_framework.config.factory import build_model_registry, build_prompt_library, build_search_planner
    from novelty_agent_framework.config.loader import legacy_shape, load_application_config
    from novelty_agent_framework.core.runtime_artifacts import RuntimeArtifactManager, RuntimeDebugConfig
    from novelty_agent_framework.schemas import NoveltyPoint, ResearchTask

    data = json.loads(args.source.read_text())
    point = NoveltyPoint.model_validate(next(p for p in data['novelty_points'] if p['point_id'] == args.point))
    point = point.model_copy(update={'claim_en': '', 'technical_features_en': []})
    task = ResearchTask(task_id=f"{data['paper_id']}-{args.point}-A-1-zh", novelty_point_id=point.point_id,
                        task_type='literature_search', language='zh')
    cfg = load_application_config()
    registry = build_model_registry(cfg)
    agent = build_search_planner(legacy_shape(cfg), registry, build_prompt_library())
    agent.concept_review_protocol = args.protocol
    if args.strategy_review:
        agent.strategy_review = True
    profile = registry.client_for(agent._model_alias).profile
    if urlparse(profile.base_url).hostname not in ('127.0.0.1', 'localhost', '::1'):
        raise ValueError('Only local model is allowed')
    if not agent.concept_review:
        raise ValueError('Concept review must be enabled')
    agent.model_client = OpenAICompatibleChatClient(profile)
    frozen_path = None
    if args.frozen_from:
        from backend.env import ModelResponse
        frozen_directory = args.frozen_from/args.source.parent.name/args.point/'planner-attempts'
        frozen_path = next(frozen_directory.iterdir())/'planner/calls/call-00001.json'
        frozen = json.loads(frozen_path.read_text())['output']
        underlying = agent.model_client
        class ReplayFirstClient:
            pending = True
            async def acomplete(self, messages, *, options=None):
                if self.pending:
                    self.pending = False
                    return ModelResponse(content=frozen, raw={'planner_replay': True})
                return await underlying.acomplete(messages, options=options)
        agent.model_client = ReplayFirstClient()
    manager = RuntimeArtifactManager(data['paper_id'], config=RuntimeDebugConfig(
        output_root=args.output, archive_root=args.output/'runtime-archive', max_model_calls=24),
        runtime_config={'max_model_calls': 24})
    result = dict(paper_id=data['paper_id'], point_id=args.point, source=str(args.source),
                  source_sha256=hashlib.sha256(args.source.read_bytes()).hexdigest(),
                  model=cfg.search_planner.model.model_dump(), scenario='A', scope='full_planner',
                  protocol=args.protocol,
                  strategy_review=agent.strategy_review,
                  generation_replayed=bool(frozen_path), frozen_source=str(frozen_path) if frozen_path else None,
                  frozen_sha256=hashlib.sha256(frozen_path.read_bytes()).hexdigest() if frozen_path else None,
                  human_semantic_review='pending', status='running')
    started = time.monotonic()
    with manager:
        try:
            plan = await agent.aplan(point, task)
            write(args.output/'plan.json', plan.model_dump(mode='json'))
            summary_path = next(args.output.glob('planner-attempts/*/planner/summary.json'))
            audit_status = json.loads(summary_path.read_text())['status']
            result.update(status=audit_status, concepts=len(plan.concepts), strategies=len(plan.strategies))
        except Exception as exc:
            result.update(status='failed', error=str(exc), error_type=type(exc).__name__)
            manager.finish_run('FAILED', error=exc)
        else:
            manager.finish_run('SUCCESS')
    result['elapsed_seconds'] = round(time.monotonic()-started, 2)
    write(args.output/'result.json', result)
    print(json.dumps(result, ensure_ascii=False), flush=True)


def batch(args):
    papers = json.loads((args.corpus/'manifest.json').read_text())['papers']
    jobs = [(Path(p['input']), point) for p in papers for point in p['point_ids']]
    if args.papers:
        jobs = [job for job in jobs if job[0].parent.name in args.papers]
    # Validate all inputs before spending any model calls.
    from novelty_agent_framework.schemas import NoveltyPoint
    for source, point in jobs:
        data = json.loads(source.read_text())
        NoveltyPoint.model_validate(next(p for p in data['novelty_points'] if p['point_id'] == point))
    tracked_diff = subprocess.check_output(['git', 'diff', 'HEAD', '--']).decode()
    manifest = dict(commit=subprocess.check_output(['git', 'rev-parse', 'HEAD']).decode().strip(),
                    branch=subprocess.check_output(['git', 'branch', '--show-current']).decode().strip(),
                    runner_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                    tracked_diff_sha256=hashlib.sha256(tracked_diff.encode()).hexdigest(),
                    cases=len(jobs), concurrency=args.workers, per_case_call_limit=24,
                    scenario='A', protocol=args.protocol, strategy_review_requested=args.strategy_review,
                    frozen_from=str(args.frozen_from) if args.frozen_from else None, results=[])
    args.output.mkdir(parents=True, exist_ok=False)
    write(args.output/'batch.json', manifest)

    def execute(job):
        source, point = job
        output = args.output/source.parent.name/point
        output.mkdir(parents=True)
        with (output/'console.log').open('w') as stream:
            command = [sys.executable, __file__, '--source', str(source), '--point', point,
                       '--output', str(output), '--protocol', args.protocol]
            if args.frozen_from:
                command.extend(['--frozen-from', str(args.frozen_from)])
            if args.strategy_review:
                command.append('--strategy-review')
            proc = subprocess.run(command, stdout=stream, stderr=subprocess.STDOUT)
        if (output/'result.json').exists():
            return json.loads((output/'result.json').read_text()) | {'output':str(output), 'exit_code':proc.returncode}
        return dict(source=str(source), point_id=point, status='runner_failed', exit_code=proc.returncode, output=str(output))

    from concurrent.futures import as_completed
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        for future in as_completed([pool.submit(execute, job) for job in jobs]):
            result = future.result()
            manifest['results'].append(result)
            write(args.output/'batch.json', manifest)
            print(f"{len(manifest['results'])}/{len(jobs)} {result.get('paper_id', result.get('source'))} {result['point_id']} {result['status']}", flush=True)
    manifest['status'] = 'finished'
    write(args.output/'batch.json', manifest)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--corpus', type=Path, default=Path('docs/experiments/planner-concept-stage/corpus-14'))
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--source', type=Path)
    parser.add_argument('--point')
    parser.add_argument('--workers', type=int, default=4, choices=(1,2,3,4))
    parser.add_argument('--protocol', choices=('v1', 'v2'), default='v1')
    parser.add_argument('--strategy-review', action='store_true', help='Enable independently reviewed per-level AND/OR strategies')
    parser.add_argument('--frozen-from', type=Path, help='Replay first generation response from a previous batch; all later calls are live')
    parser.add_argument('--papers', nargs='+', help='Optional paper directory names for focused natural trials')
    args = parser.parse_args()
    if args.source:
        asyncio.run(case(args))
    else:
        batch(args)
