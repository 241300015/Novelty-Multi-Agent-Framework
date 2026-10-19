"""Repeat the production extraction node in fresh processes, using local models."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'backend/src'), str(ROOT)]
from novelty_agent_framework.config import load_application_config, build_standard_full_workflow
from novelty_agent_framework.config.experiment import prepare_startup
from novelty_agent_framework.core.paper_reading import PaperReader, latest_reading_reference
from novelty_agent_framework.core.runtime_artifacts import RuntimeArtifactManager, RuntimeDebugConfig
from novelty_agent_framework.persistence import persist_workflow_input
from novelty_agent_framework.schemas import PaperInput


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--paper-json', type=Path, required=True)
    parser.add_argument('--profile', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--repetitions', type=int, default=5)
    parser.add_argument('--workspace', type=Path)
    args = parser.parse_args()
    config = load_application_config(profile_path=args.profile, environ={})
    model = config.models[config.point_extractor.model.alias]
    if model.base_url != 'http://127.0.0.1:8000/v1':
        parser.error('This experiment permits only the local model endpoint')
    root = args.output.resolve()
    if args.workspace is None:
        root.mkdir(parents=True, exist_ok=False)
        paper = PaperInput.model_validate_json(args.paper_json.read_text())
        workspace = persist_workflow_input(paper, output_root=root / 'input')
        prepare_startup(config, entrypoint='paper_input', input_path=args.paper_json,
            output_root=root, snapshot_dir=root / 'startup',
            input_contents={'paper-input.json': paper.model_dump_json().encode()})
        (root / 'runner.py').write_bytes(Path(__file__).read_bytes())
        rows = []
        for i in range(args.repetitions):
            trial = root / f'{i + 1:02d}'
            completed = subprocess.run([sys.executable, str(Path(__file__).resolve()),
                '--paper-json', str(args.paper_json), '--profile', str(args.profile),
                '--output', str(trial), '--workspace', str(workspace)])
            result = json.loads((trial / 'result.json').read_text())
            rows.append({'trial': trial.name, 'exit_code': completed.returncode, **result})
            write(root / 'results.json', rows)
        return int(any(r['exit_code'] for r in rows))

    root.mkdir(parents=True, exist_ok=False)
    paper, _ = PaperReader(latest_reading_reference(args.workspace)).load()
    workflow = build_standard_full_workflow(config, output_root=root)
    manager = RuntimeArtifactManager(paper.paper_id, config=RuntimeDebugConfig(
        output_root=root, archive_root=root / 'archive', max_model_calls=config.project.runtime_debug.max_model_calls),
        model_provider='openai_compatible', model_name=model.model, stage_names=('extract_points',))
    manager.activate()
    started = time.monotonic()
    result = {'pid': os.getpid(), 'parent_pid': os.getppid(), 'model': model.model,
              'endpoint': model.base_url, 'input_workspace': str(args.workspace),
              'model_settings': config.point_extractor.model.model_dump(mode='json')}
    try:
        extracted = asyncio.run(workflow._extract_points({'paper': paper, 'run_id': manager.run_id}))
        result.update(status='completed', points=[p.model_dump(mode='json') for p in extracted['novelty_points']])
        manager.finish_run('SUCCESS')
    except Exception as exc:
        result.update(status='failed', error_type=type(exc).__name__, error=str(exc))
        manager.finish_run('FAILED', error=exc)
    finally:
        manager.deactivate()
        trace = workflow.point_extractor.last_trace
        result.update(seconds=time.monotonic() - started, execution_status=trace.get('execution_status'),
            budgets=trace.get('budgets'), corrections=trace.get('corrections'),
            read_ranges=[[r['start'], r['end']] for r in trace.get('reading', {}).get('reads', [])])
        write(root / 'trace.json', trace)
        write(root / 'result.json', result)
        print(json.dumps({k: result[k] for k in ('pid', 'status', 'seconds', 'budgets')}, ensure_ascii=False), flush=True)
    return int(result['status'] != 'completed')


if __name__ == '__main__':
    raise SystemExit(main())
