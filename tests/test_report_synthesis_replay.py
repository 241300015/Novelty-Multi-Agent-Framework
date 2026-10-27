"""Offline current-point replay and explicit historical report compatibility."""
import asyncio
import json

import pytest

from scripts.replay_report_synthesis import (
    CapturedDraftClient, CapturedPointCoordinator, CapturedReportCoordinator,
    OfflineDraftClient, _check_frozen_gate, _typed_state,
)
from novelty_agent_framework.agents import NoveltyCoordinatorAgent
from novelty_agent_framework.core.report_synthesis import CoordinatorSynthesisHarness, ReportSynthesisError
from test_report_synthesis_harness import inputs, result
from test_report_narrative_contract import _case


def test_offline_replay_uses_current_point_contract():
    client = OfflineDraftClient()
    data = inputs(2)
    report = asyncio.run(NoveltyCoordinatorAgent(client).asynthesize(**data,
        rejected_evidence=[], insufficient_final_evidence_points=[]))
    assert len(report.conclusions) == 2 and client.calls == 2
    assert all('离线' in c.summary for c in report.conclusions)


def test_historical_response_is_assembled_without_entering_point_model(tmp_path):
    data = inputs(2)
    _, _, _, draft = _case(2)
    path = tmp_path / 'captured.json'
    path.write_text(json.dumps({'status': 'SUCCESS', 'finish_reason': 'stop',
        'response': {'content': draft.model_dump_json()}}))
    fixture = CapturedDraftClient(path)
    coordinator = CapturedReportCoordinator(fixture)
    report = coordinator.synthesize(**data, rejected_evidence=[], insufficient_final_evidence_points=[])
    assert fixture.calls == 1 and report.conclusions[0].supporting_card_ids == ['P-card-0']
    assert not hasattr(coordinator, 'asynthesize')


def test_captured_points_require_same_materials_and_preserve_order(tmp_path):
    async def generate(materials, **kwargs):
        return result('综述：' + materials['novelty_point']['claim'])
    data = inputs(2)
    expected = asyncio.run(CoordinatorSynthesisHarness(generate).run(**data, output_root=tmp_path))
    path = next(tmp_path.glob('synthesis/report-synthesis/*.json'))
    replay = CapturedPointCoordinator(path)
    actual = asyncio.run(replay.asynthesize(**data, rejected_evidence=[], insufficient_final_evidence_points=[]))
    assert actual == expected and replay.calls == 2
    data['brief'].novelty_points[0].claim = 'changed input'
    with pytest.raises(ReportSynthesisError, match='input differs'):
        asyncio.run(replay.asynthesize(**data, rejected_evidence=[], insufficient_final_evidence_points=[]))


@pytest.mark.parametrize('gate', [
    {'validation_passed': False},
    {'validation_passed': True, 'accepted_card_ids': ['foreign']},
    {'validation_passed': True, 'output_review_count': 1, 'rejected_review_count': 1},
    {'validation_passed': True},
])
def test_replay_rejects_incompatible_frozen_gate(gate):
    with pytest.raises(ValueError):
        _check_frozen_gate({'synthesis_integrity': gate})


def test_replay_accepts_review_gate_and_decodes_current_frozen_state():
    data = inputs(1)
    raw = {'paper': data['paper'].model_dump(mode='json'),
        'brief': data['brief'].model_dump(mode='json'),
        'novelty_points': [x.model_dump(mode='json') for x in data['brief'].novelty_points],
        'novelty_reviews': [x.model_dump(mode='json') for x in data['novelty_reviews']],
        'evidence_cards': [x.model_dump(mode='json') for x in data['evidence']],
        'synthesis_integrity': {'validation_passed': True, 'output_review_count': 1, 'rejected_review_count': 0}}
    _check_frozen_gate(raw)
    state = _typed_state(raw)
    assert state['novelty_reviews'] == data['novelty_reviews']
    assert state['raw_evidence'] == [] and state['failure_events'] == []


def test_offline_replay_completes_report_nodes_with_registered_artifacts(tmp_path, monkeypatch):
    import hashlib
    from dataclasses import replace
    from scripts import replay_report_synthesis as script
    from novelty_agent_framework.workflows import NoveltyWorkflow

    data = inputs(2)
    raw = {'paper': data['paper'].model_dump(mode='json'),
        'brief': data['brief'].model_dump(mode='json'),
        'novelty_points': [x.model_dump(mode='json') for x in data['brief'].novelty_points],
        'novelty_reviews': [x.model_dump(mode='json') for x in data['novelty_reviews']],
        'evidence_cards': [x.model_dump(mode='json') for x in data['evidence']],
        'synthesis_integrity': {'validation_passed': True, 'output_review_count': 2, 'rejected_review_count': 0}}
    source = tmp_path / 'frozen.json'
    source.write_text(json.dumps(raw))
    root = tmp_path / 'recovery'
    row = {'label': 'fixture', 'synthesize_stage_input': str(source),
        'synthesize_stage_input_sha256': hashlib.sha256(source.read_bytes()).hexdigest(),
        'paper_id': 'synthesis', 'source_run_id': 'old', 'recovery_id': 'replay',
        'recovery_output_root': str(root)}
    def copy_inputs(source, source_run_id, output_root, paper_id):
        workspace = output_root / paper_id
        for name, value in {
            'novelty-points.json': {'paper_id': paper_id, 'novelty_points': raw['novelty_points']},
            'retrieval-plans.json': {'paper_id': paper_id, 'novelty_point_plans': []},
            'evidence-cards.json': {'paper_id': paper_id, 'accepted_evidence_cards': raw['evidence_cards'],
                                   'raw_evidence_cards': raw['evidence_cards']},
        }.items():
            path = workspace / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(value))
    def workflow(*, output_root):
        services = replace(NoveltyWorkflow.default().services, coordinator=NoveltyCoordinatorAgent())
        return NoveltyWorkflow(services, output_root=output_root)
    monkeypatch.setattr(script, '_copy_renderer_inputs', copy_inputs)
    monkeypatch.setattr(script, 'build_workflow', workflow)
    replay = asyncio.run(script._one(row, 'offline', 30))
    assert replay['status'] == 'SUCCESS', replay
    assert replay['local_response_replay_calls'] == 2
    assert len(replay['recomputed_stages']) == 4
    assert (root / 'synthesis/report-integrity.json').is_file()
    index = json.loads((root / 'synthesis/report-inputs.json').read_text())
    assert index['run_id'] == 'replay' and index['status'] == 'ready'
    assert any(path.startswith('report-synthesis/') for path in index['artifacts'])
