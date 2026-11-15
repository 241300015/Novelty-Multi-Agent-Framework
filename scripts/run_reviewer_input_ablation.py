"""Paired real local-model single-card experiment; no retrieval or point synthesis.

The baseline uses the default production input. Only the stripped arm installs
an experiment-local projection; persisted domain objects stay unchanged.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import ExitStack
from copy import deepcopy
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import time
from types import SimpleNamespace
from unittest.mock import patch
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'backend/src')]

from novelty_agent_framework.agents import evidence_reviewer
from novelty_agent_framework.agents.reviewer_checkpoint import _source_snapshot
from novelty_agent_framework.config import load_application_config, build_standard_full_workflow
from novelty_agent_framework.core import reviewer_choices
from novelty_agent_framework.schemas import NoveltyPointReviewRequest
from novelty_agent_framework.workflows import reviewer_reading_policy


def reviewer_card_model_view(value):
    """Historical ablation whitelist, deliberately confined to experiments."""
    result = deepcopy(value)
    card_fields = {"card_id", "task_id", "novelty_point_id", "document_title", "sources", "evidence_ids"}
    evidence_fields = {"evidence_id", "work_id", "artifact_id", "novelty_point_id", "task_id", "quote"}
    result["cards"] = [{key: item for key, item in card.items() if key in card_fields}
                       for card in result["cards"]]
    result["evidence"] = [{key: item for key, item in evidence.items() if key in evidence_fields}
                          for evidence in result["evidence"]]
    return result


class _StrippedInputChoices(reviewer_choices.ReviewerChoices):
    def project(self, value):
        # Intercept only the initial card payload, before identity projection.
        # Menus, reading receipts, bindings and domain requests stay unchanged.
        if isinstance(value, dict) and {"novelty_point", "cards", "evidence"} <= value.keys():
            value = reviewer_card_model_view(value)
        return super().project(value)


def stripped_card_input():
    """Use only around sequential experiment calls; restore the class on exit."""
    return patch.object(reviewer_reading_policy, 'ReviewerChoices', _StrippedInputChoices)


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str) + '\n')


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


async def trial(args, checkpoint, pair, arm):
    destination = args.output / f'pair-{pair}-{arm}'
    destination.mkdir(exist_ok=False)
    request = NoveltyPointReviewRequest.model_validate(checkpoint['request'])
    if len(request.cards) != 1:
        raise ValueError('This paired experiment requires one frozen Card')
    before = request.model_dump(mode='json')
    paper_root = destination / request.subject_paper_id
    paper_root.mkdir()
    for name in ['references', 'subject_references']:
        source = args.source_run / request.subject_paper_id / name
        if source.exists():
            shutil.copytree(source, paper_root / name)
    if _source_snapshot(request, checkpoint['card_results'], destination) != checkpoint['source_snapshot']:
        raise ValueError('Copied source differs from the frozen source snapshot')
    # Startup binds configuration to its output directory. Each trial needs an
    # independent instance loaded from the same immutable configuration files.
    config = load_application_config(profile_path=args.profile, models_path=args.models, environ={})
    reviewer = build_standard_full_workflow(config, output_root=destination).services.reviewer
    inner = reviewer._client()
    if inner.profile.base_url != 'http://127.0.0.1:8000/v1' or inner.profile.model != 'qwen2.5-7b-instruct':
        raise ValueError('Experiment permits only the configured local Qwen model')
    calls, events = [], []

    class Recorder:
        async def acomplete(self, messages, *, options=None):
            item = {'request_payload': inner._build_payload(messages, options), 'status': 'RUNNING'}
            calls.append(item)
            save(destination / 'calls.json', calls)
            try:
                response = await inner.acomplete(messages, options=options)
                item.update(status='SUCCESS', response={'content': response.content,
                    'usage': dict(response.usage),
                    'tool_calls': [asdict(c) for c in response.tool_calls]})
                return response
            except Exception as exc:
                item.update(status='FAILED', error_type=type(exc).__name__, error=str(exc)[:1000])
                raise
            finally:
                save(destination / 'calls.json', calls)

    def record(phase, request, payload):
        events.append({'phase': phase, 'point_id': request.novelty_point.point_id, **payload})
        save(destination / 'events.json', events)

    reviewer.model_client = Recorder()
    started = time.monotonic()
    with ExitStack() as stack:
        # Same short option identifiers within each pair remove irrelevant UUID
        # differences; each run still has a fresh ledger and independent state.
        stack.enter_context(patch.object(reviewer_choices, 'secrets',
            SimpleNamespace(token_hex=lambda n: f'{pair:08x}')))
        stack.enter_context(patch.object(reviewer_reading_policy, 'uuid',
            SimpleNamespace(uuid4=lambda: uuid.UUID(int=pair))))
        stack.enter_context(patch.object(evidence_reviewer, '_record_review_event', record))
        if arm == 'stripped':
            stack.enter_context(stripped_card_input())
        review = await reviewer.review_card(request)
    save(destination / 'review.json', review.model_dump(mode='json'))
    if request.model_dump(mode='json') != before:
        raise AssertionError('Domain request was modified')
    result = {'pair': pair, 'arm': arm, 'status': review.status.value,
        'incomplete_reason': review.incomplete_reason, 'duration_seconds': time.monotonic() - started,
        'model_calls': len(calls), 'support_status': review.support_status,
        'support_reason': review.support_reason,
        'feature_comparisons': [f.model_dump(mode='json') for f in review.feature_comparisons],
        'supplement_request': review.supplement_request.model_dump(mode='json') if review.supplement_request else None,
        'read_history': review.harness_state.get('read_history'),
        'transitions': review.harness_state.get('transitions'),
        'target_claim_prefix_in_support_reason': review.support_reason.startswith('该工作' + request.novelty_point.claim),
        'input_unchanged': True,
        'source_unchanged': _source_snapshot(request, checkpoint['card_results'], destination) == checkpoint['source_snapshot']}
    save(destination / 'result.json', result)
    print(json.dumps({k: result[k] for k in ['pair', 'arm', 'status', 'model_calls',
        'target_claim_prefix_in_support_reason']}, ensure_ascii=False), flush=True)
    return result


def audit_pair(output, pair):
    a = json.loads((output / f'pair-{pair}-baseline/calls.json').read_text())
    b = json.loads((output / f'pair-{pair}-stripped/calls.json').read_text())
    pa, pb = a[0]['request_payload'], b[0]['request_payload']
    ua, ub = json.loads(pa['messages'][1]['content']), json.loads(pb['messages'][1]['content'])
    expected = json.loads(json.dumps(ua))
    for card in expected['cards']:
        for field in ['main_contribution', 'overlaps', 'differences', 'cited_by_paper',
                      'possible_baseline', 'relevance', 'confidence']:
            card.pop(field, None)
    for evidence in expected['evidence']:
        for field in ['interpretation', 'confidence']:
            evidence.pop(field, None)
    assert expected == ub
    for k in set(pa) | set(pb):
        if k != 'messages':
            assert pa.get(k) == pb.get(k), k
    assert pa['messages'][:1] == pb['messages'][:1]
    assert pa['messages'][2:] == pb['messages'][2:]
    # Every later stripped request must carry the same cleaned initial input.
    for call in b:
        messages = call['request_payload']['messages']
        if not messages[0]['content'].startswith('Repair the format'):
            assert json.loads(messages[1]['content']) == ub
    return {'pair': pair, 'initial_request_diff_is_only_judgment_removal': True,
        'baseline_input_characters': len(pa['messages'][1]['content']),
        'stripped_input_characters': len(pb['messages'][1]['content']),
        'removed_card_fields': sorted(set(ua['cards'][0]) - set(ub['cards'][0])),
        'removed_evidence_fields': sorted(set(ua['evidence'][0]) - set(ub['evidence'][0]))}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ['checkpoint', 'source-run', 'profile', 'models', 'output']:
        parser.add_argument('--' + name, required=True, type=Path)
    parser.add_argument('--pairs', type=int, default=3, choices=range(1, 4))
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    checkpoint = json.loads(args.checkpoint.read_text())
    request = NoveltyPointReviewRequest.model_validate(checkpoint['request'])
    if _source_snapshot(request, checkpoint['card_results'], args.source_run) != checkpoint['source_snapshot']:
        raise ValueError('Historical source has changed')
    source_hashes = {str(p.relative_to(ROOT)): sha(p) for p in (ROOT / 'backend').rglob('*')
                     if p.is_file() and p.suffix in {'.py', '.md', '.json'} and '__pycache__' not in p.parts}
    save(args.output / 'source.json', {'git_commit': subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip(),
        'checkpoint': str(args.checkpoint), 'checkpoint_sha256': sha(args.checkpoint),
        'profile_sha256': sha(args.profile), 'model_registry_sha256': sha(args.models),
        'source_sha256': source_hashes, 'pairs': args.pairs,
        'experiment_script_sha256': sha(Path(__file__)),
        'input_projection': 'baseline=production; stripped=experiment_whitelist',
        'design': 'Full controlled single-card review, sequential paired A/B, then B/A, then A/B. Only input projection differs.',
        'source_snapshot_verified': True, 'new_retrieval': False, 'paid_api': False})
    (args.output / 'working.diff').write_bytes(subprocess.check_output(['git', 'diff']))
    results = []
    async def run_all():
        for pair in range(1, args.pairs + 1):
            order = ['baseline', 'stripped'] if pair % 2 else ['stripped', 'baseline']
            for arm in order:
                results.append(await trial(args, checkpoint, pair, arm))
                save(args.output / 'results.json', results)
    asyncio.run(run_all())
    audit = [audit_pair(args.output, pair) for pair in range(1, args.pairs + 1)]
    assert all(sha(ROOT / f) == h for f, h in source_hashes.items()), 'Production code changed during experiment'
    save(args.output / 'pair-audit.json', audit)
    print(json.dumps({'completed_trials': len(results), 'pair_audit': audit}, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
