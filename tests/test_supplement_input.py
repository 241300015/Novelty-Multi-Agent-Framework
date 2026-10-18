"""Compact planning contracts and full-record persistence locators."""
import asyncio
import hashlib
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from novelty_agent_framework.persistence import (
    persist_evidence_cards, persist_novelty_points, persist_novelty_reviews,
    persist_task_research_result, persist_workflow_input,
)
from novelty_agent_framework.schemas.supplement_planning import SupplementPlanningInput
from novelty_agent_framework.core.supplement_input import persisted_material_index
from test_supplement_coordinator import setup


@pytest.fixture(autouse=True)
def isolate(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)


def plan(*, persist=False, stale=False):
    workflow, state, client = setup('semantic')
    if persist:
        persist_workflow_input(state['paper'])
        persist_novelty_points(state['paper'], state['brief'].novelty_points)
        persist_evidence_cards(state['paper'], raw_cards=state['evidence_cards'],
                               accepted_cards=state['evidence_cards'], rejected_evidence=[])
        persist_novelty_reviews(state['paper'], state['novelty_reviews'])
        for result in state['task_research_results']:
            persist_task_research_result(state['paper'].paper_id, result, attempt=1)
        if stale:
            path = Path('outputs/paper-test/paper-input/others/paper.json')
            data = json.loads(path.read_text())
            data['full_text'] = 'outdated full paper from a previous run'
            path.write_text(json.dumps(data))
    checked = asyncio.run(workflow._check_final_evidence_sufficiency(state))
    result = asyncio.run(workflow._plan_supplement({**state, **checked}))
    assert result['research_tasks']
    path = Path('outputs/paper-test/recovery/run-supplement-test/attempt-2-input.json')
    return SupplementPlanningInput.model_validate_json(path.read_text()), state, client


def test_persisted_full_records_have_resolvable_verified_indexes_and_compact_prompt():
    request, state, client = plan(persist=True)
    assert {r.kind for r in request.material_index} == {
        'paper', 'novelty_point', 'evidence_card', 'task_result', 'review', 'recovery_decision'}
    prompt = client.calls[0][1].content
    assert state['paper'].full_text not in prompt
    assert 'SupplementPlanningInput v2' in prompt
    for ref in request.material_index:
        raw = (Path('outputs') / ref.path).read_bytes()
        assert hashlib.sha256(raw).hexdigest() == ref.sha256
        value = json.loads(raw)
        for token in ref.json_pointer.split('/')[1:]:
            token = token.replace('~1', '/').replace('~0', '~')
            value = value[int(token)] if isinstance(value, list) else value[token]
        assert value
        assert ref.path in prompt
        if ref.kind == 'paper':
            assert value['full_text'] == state['paper'].full_text
        if ref.kind == 'evidence_card':
            assert value == state['evidence_cards'][0].model_dump(mode='json')
    serialized = request.model_dump(mode='json')
    assert 'full_text' not in serialized['paper']
    assert 'sources' not in serialized['existing_evidence'][0]


def test_missing_files_are_not_reported_as_persisted():
    request, _, _ = plan()
    assert {ref.kind for ref in request.material_index} == {'recovery_decision'}


def test_stale_paper_with_same_id_is_not_reported_as_current_material():
    request, _, _ = plan(persist=True, stale=True)
    assert 'paper' not in {r.kind for r in request.material_index}
    assert 'evidence_card' in {r.kind for r in request.material_index}


def test_array_reordering_and_unreadable_files_do_not_create_false_indexes():
    request, state, _ = plan(persist=True)
    workspace = Path('outputs/paper-test')
    cards_path = workspace / 'evidence-cards.json'
    data = json.loads(cards_path.read_text())
    data['accepted_evidence_cards'].insert(0, {'card_id': 'unrelated'})
    cards_path.write_text(json.dumps(data))
    (workspace / 'novelty-points.json').write_text('invalid json')
    refs = persisted_material_index(output_root='outputs', workspace=workspace,
        paper=state['paper'], points=state['brief'].novelty_points, cards=state['evidence_cards'],
        tasks=state['brief'].research_tasks, results=state['task_research_results'],
        reviews=state['novelty_reviews'], decisions=request.recovery_decisions,
        run_id=request.run_id, round_number=1)
    assert 'novelty_point' not in {r.kind for r in refs}
    assert next(r for r in refs if r.kind == 'evidence_card').json_pointer == '/accepted_evidence_cards/1'


@pytest.mark.parametrize('mutation', [
    lambda d: d['paper'].update(full_text='must not be sent'),
    lambda d: d['recovery_decisions'][0].update(action='stop', max_additional_attempts=0),
    lambda d: d.update(attempt=3),
    lambda d: d.update(research_languages=['en', 'en']),
    lambda d: d['existing_evidence'][0].update(novelty_point_id='not-authorized'),
    lambda d: d['brief']['novelty_points'][0]['feature_catalog'].append(
        dict(feature_id='F999', text='invented', sha256='0' * 64)),
    lambda d: d['material_index'][0].update(path='../other/paper.json'),
    lambda d: d['material_index'][0].update(json_pointer='/bad~escape'),
    lambda d: d['material_index'][0].update(kind='task_result', point_id='NP-1', task_id='T-1'),
])
def test_contract_rejects_invalid_or_unauthorized_input(mutation):
    request, _, _ = plan(persist=True)
    data = request.model_dump(mode='json')
    mutation(data)
    with pytest.raises(ValidationError):
        SupplementPlanningInput.model_validate(data)


def test_count_gap_is_explicit_and_checked():
    workflow, state, _ = setup('count')
    checked = asyncio.run(workflow._check_final_evidence_sufficiency(state))
    asyncio.run(workflow._plan_supplement({**state, **checked}))
    path = Path('outputs/paper-test/recovery/run-supplement-test/attempt-2-input.json')
    data = json.loads(path.read_text())
    gap = data['insufficient_final_evidence_points'][0]
    assert gap['additional_cards_needed'] == gap['required_card_count'] - gap['valid_card_count']
    gap['additional_cards_needed'] += 1
    with pytest.raises(ValidationError):
        SupplementPlanningInput.model_validate(data)
