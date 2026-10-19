"""Local-only Extractor experiment; prepare input, then read it in a fresh process.

No remote model fallback. Raw local responses and injected faults are distinguished.
"""
from __future__ import annotations

import argparse
from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'backend/src'), str(ROOT)]

from backend.env import ModelCallOptions, ModelProfile, OpenAICompatibleChatClient, PromptLibrary
from novelty_agent_framework.agents.point_extractor import NoveltyPointExtractorAgent, build_paper_digest
from novelty_agent_framework.core.extractor_harness import ExtractorHarnessConfig
from novelty_agent_framework.core.paper_reading import PaperReader, latest_reading_reference
from novelty_agent_framework.core.runtime_artifacts import RuntimeArtifactManager, RuntimeDebugConfig
from novelty_agent_framework.persistence import persist_workflow_input
from novelty_agent_framework.schemas import PaperInput


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def sample(case):
    if case == 'real_paper':
        return PaperInput.model_validate_json((ROOT / 'tests/fixtures/extractor/real_paper_input.json').read_text())
    return PaperInput(paper_id='controlled-late-method', title='Event-triggered graph scheduling',
        abstract='This paper studies dynamic graph scheduling.',
        full_text=('INTRODUCTION: background and established baselines only. ' * 150)[:4000]
                  + ('RELATED WORK: conventional static scheduling methods. ' * 150)[:4000]
                  + 'LATE METHODS: We propose HELIOS-LATE, an event-triggered graph scheduler. '
                    'Its single contribution couples uncertainty-gated queue partitioning with a '
                    'rollback-safe temporal consistency rule. These two coupled steps constitute '
                    'one scheduling mechanism, not independent algorithms. Performance numbers '
                    'are evaluation results, not separate contributions.')


def freeze(output, args):
    files = []
    for folder in ('backend/src/novelty_agent_framework', 'backend/env'):
        files.extend(p for p in (ROOT / folder).rglob('*') if p.suffix in {'.py', '.json', '.md'} and '__pycache__' not in p.parts)
    files += [Path(__file__), ROOT / 'config/profiles/extractor-harness-local.json', ROOT / 'tests/test_extractor_controlled.py']
    entries = []
    for path in files:
        relative = path.relative_to(ROOT)
        raw = path.read_bytes()
        dest = output / 'source' / relative
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(raw)
        entries.append({'path': str(relative), 'sha256': hashlib.sha256(raw).hexdigest()})
    write(output / 'manifest.json', {'created_at': datetime.now(timezone.utc).isoformat(),
        'branch': subprocess.check_output(['git', 'branch', '--show-current'], cwd=ROOT, text=True).strip(),
        'base_commit': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
        'model': 'qwen2.5-7b-instruct', 'endpoint': 'http://127.0.0.1:8000/v1',
        'case': args.case, 'mode': args.mode, 'injection': args.inject, 'files': entries,
        'pinned_reader_segment': args.reader_segment,
        'python': sys.version, 'model_options': {'temperature': 0.0, 'max_tokens': 3072, 'timeout_seconds': 180},
        'budget': {'generate': 3, 'review': 3, 'read': 3, 'read_chars': 20000, 'read_segments': 24}})


class RecordingClient:
    def __init__(self, base, output, injection, reader_segment=None):
        self.base, self.output, self.injection = base, output, injection
        self.reader_segment = reader_segment
        self.injected = False
        self.records = []

    def complete(self, messages, *, options=None):
        started = time.monotonic()
        response = self.base.complete(messages, options=options)
        raw = response.content
        text = messages[-1].content
        context = json.loads(text[text.index('{'):]) if 'Harness 输入' in text else {}
        phase = context.get('phase', 'legacy')
        applied = False
        pinned = False
        if self.reader_segment and phase == 'reading_scope':
            option = next(o for o in context['options'] if o.get('segment_id') == self.reader_segment)
            response = replace(response, content=json.dumps({'option_ids': [option['option_id']],
                'reason': 'Evaluation supplies this required segment to isolate persisted late-content consumption from autonomous scope selection.'}))
            pinned = True
        if not self.injected:
            if (self.injection == 'invalid_reader' and phase == 'reading_scope'):
                response = replace(response, content=json.dumps({'option_ids': ['invented-path'], 'reason': 'injected invalid choice'}))
                applied = True
            elif (self.injection == 'invalid_generation' and phase == 'generate') or (self.injection == 'invalid_review' and phase == 'semantic_review'):
                response = replace(response, content='{invalid json')
                applied = True
            elif self.injection == 'duplicate' and phase == 'generate':
                value = json.loads(raw)
                if value.get('points'):
                    duplicate = dict(value['points'][0])
                    duplicate['claim'] += '（同一机制的另一种表述）'
                    value['points'].append(duplicate)
                    response = replace(response, content=json.dumps(value, ensure_ascii=False))
                    applied = True
        self.injected |= applied
        record = {'phase': phase, 'duration_seconds': time.monotonic() - started,
                  'messages': [{'role': m.role, 'content': m.content} for m in messages],
                  'raw_model_output': raw, 'harness_visible_output': response.content,
                  'reader_selection_overridden': pinned,
                  'injected_fault': self.injection if applied else None, 'usage': dict(response.usage)}
        self.records.append(record)
        write(self.output / 'calls' / f'{len(self.records):03d}.json', record)
        print(json.dumps({'call': len(self.records), 'phase': phase, 'seconds': round(record['duration_seconds'], 2),
                          'injected': applied}, ensure_ascii=False), flush=True)
        return response


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--case', choices=['long_tail', 'real_paper'], default='long_tail')
    parser.add_argument('--mode', choices=['controlled', 'baseline'], default='controlled')
    parser.add_argument('--inject', choices=['none', 'invalid_reader', 'invalid_generation', 'invalid_review', 'duplicate'], default='none')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--reader-segment', help='Evaluation-only pinned legal Reader choice; not autonomous selection')
    parser.add_argument('--workspace', type=Path)
    args = parser.parse_args()
    if args.reader_segment and (args.mode != 'controlled' or args.inject != 'none'):
        parser.error('Pinned reading is a separate controlled experiment, not a fault injection or baseline mode')
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    if args.workspace is None:
        if (output / 'manifest.json').exists():
            parser.error('Use a new output directory to preserve prior experiment evidence')
        workspace = persist_workflow_input(sample(args.case), output_root=output / 'inputs')
        freeze(output, args)
        # Only the persisted path crosses the process boundary, never PaperInput/body.
        command = [sys.executable, str(Path(__file__).resolve()), '--case', args.case,
            '--mode', args.mode, '--inject', args.inject, '--output', str(output), '--workspace', str(workspace)]
        if args.reader_segment:
            command += ['--reader-segment', args.reader_segment]
        return subprocess.call(command)

    reference = latest_reading_reference(args.workspace)
    paper, _ = PaperReader(reference).load()
    digest = build_paper_digest(paper).model_copy(update={'paper_source': reference})
    manager = RuntimeArtifactManager(paper.paper_id, config=RuntimeDebugConfig(
        output_root=output / 'runtime', archive_root=output / 'archive', max_model_calls=16),
        model_provider='openai_compatible', model_name='qwen2.5-7b-instruct', stage_names=('extract_points',))
    manager.activate()
    manager.record_paper_reading_index(reference.model_dump(mode='json'))
    profile = ModelProfile(alias='extractor-local', model='qwen2.5-7b-instruct',
        base_url='http://127.0.0.1:8000/v1', api_key=os.getenv('LOCAL_VLLM_API_KEY', 'local'),
        context_window=32768, defaults={'temperature': 0.0, 'timeout_seconds': 180})
    client = RecordingClient(OpenAICompatibleChatClient(profile), output, args.inject, args.reader_segment)
    agent = NoveltyPointExtractorAgent(client, harness_config=ExtractorHarnessConfig(enabled=args.mode == 'controlled'),
        prompts=PromptLibrary(ROOT / 'backend/src/novelty_agent_framework/prompts'),
        model_options=ModelCallOptions(temperature=0.0, max_tokens=3072, timeout_seconds=180))
    start = time.monotonic()
    result = {'case': args.case, 'mode': args.mode, 'injection': args.inject,
              'pinned_reader_segment': args.reader_segment, 'autonomous_reading_selection': not bool(args.reader_segment),
              'model': profile.model, 'source_chars': len(paper.full_text), 'fresh_process': True,
              'input_reference': reference.model_dump(mode='json'), 'digest_has_late_marker': 'HELIOS-LATE' in digest.full_text_excerpt}
    try:
        points = agent.extract(digest, previous_brief=None, attempt=1)
        result.update(status='completed', points=[p.model_dump(mode='json') for p in points])
        manager.finish_run('SUCCESS')
    except Exception as exc:
        result.update(status='failed', error_type=type(exc).__name__, error=str(exc))
        manager.finish_run('FAILED', error=exc)
    finally:
        manager.deactivate()
        trace = agent.last_trace
        result.update(duration_seconds=time.monotonic() - start, calls=len(client.records),
            injection_applied=client.injected, execution_status=trace.get('execution_status'),
            budgets=trace.get('budgets'), coverage_complete=trace.get('coverage_complete'),
            usage={k: sum(r['usage'].get(k, 0) or 0 for r in client.records)
                   for k in ('prompt_tokens', 'completion_tokens', 'total_tokens')})
        write(output / 'trace.json', trace)
        write(output / 'result.json', result)
        print(json.dumps({k: result[k] for k in ('status', 'calls', 'duration_seconds', 'usage')}, ensure_ascii=False), flush=True)
    return 0 if result['status'] == 'completed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
