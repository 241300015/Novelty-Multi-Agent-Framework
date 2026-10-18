"""Initial work and supplements share the Coordinator -> Planner task gate."""
import asyncio
import json
from dataclasses import replace
from pathlib import Path

import pytest

from novelty_agent_framework.agents import DemoCoordinator, NoveltyCoordinatorAgent
from novelty_agent_framework.core.recovery_planning import validate_supplement_tasks
from novelty_agent_framework.schemas import NoveltyBrief, NoveltyPoint, ResearchTask
from novelty_agent_framework.schemas.failures import CodedFailure, FailureCode as C, RecoveryDecision
from test_workflow import build_workflow, make_paper, RecordingPlanner


def inputs(kind):
    points = [NoveltyPoint(point_id='NP-1', claim='first mechanism'),
              NoveltyPoint(point_id='NP-2', claim='second mechanism')]
    initial = kind == 'initial'
    tasks = [ResearchTask(task_id='T-1', novelty_point_id=p.point_id, language='en',
                          task_type='literature_search' if initial else 'research_gap',
                          description='Search mechanism comparisons', attempt=1 if initial else 2)
             for p in points]
    brief = NoveltyBrief(paper_summary='summary', novelty_points=points, research_tasks=tasks)
    return tasks, dict(brief=brief, planning_type=kind, languages=['en'],
        attempt=1 if initial else 2, trigger_code=None if initial else 'evidence.missing',
        decisions=[] if initial else [RecoveryDecision(point_id=p.point_id,
            action='research_gap', reason='missing evidence') for p in points])


@pytest.mark.parametrize('kind', ['initial', 'supplement'])
@pytest.mark.parametrize('case,code', [
    ('unknown_point', C.COORDINATOR_POINT_SCOPE),
    ('duplicate_point', C.COORDINATOR_POINT_DUPLICATE),
    ('duplicate_task', C.COORDINATOR_TASK_DUPLICATE),
    ('language', C.COORDINATOR_TASK_LANGUAGE),
    ('type', C.COORDINATOR_TASK_TYPE),
    ('description', C.COORDINATOR_TASK_DESCRIPTION),
    ('coverage', C.COORDINATOR_TASK_COVERAGE),
    ('empty', C.COORDINATOR_TASK_COVERAGE),
])
def test_same_invalid_tasks_produce_same_codes_in_both_rounds(kind, case, code):
    tasks, kwargs = inputs(kind)
    if case == 'duplicate_point':
        kwargs['brief'] = kwargs['brief'].model_copy(update={
            'novelty_points': [*kwargs['brief'].novelty_points, kwargs['brief'].novelty_points[0]]})
    elif case == 'duplicate_task':
        tasks.append(tasks[0])
    elif case == 'coverage':
        tasks.pop()
    elif case == 'empty':
        tasks = []
    else:
        updates = {'unknown_point': {'novelty_point_id': 'NP-missing'},
                   'language': {'language': 'zh'}, 'type': {'task_type': 'read_artifact'},
                   'description': {'description': '  '}}[case]
        tasks[0] = tasks[0].model_copy(update=updates)
    with pytest.raises(CodedFailure) as error:
        validate_supplement_tasks(tasks, **kwargs)
    assert error.value.code == code
    assert code.value in str(error.value)


@pytest.mark.parametrize('kind', ['initial', 'supplement'])
def test_valid_tasks_keep_point_bindings_and_runtime_identity(kind):
    tasks, kwargs = inputs(kind)
    result = validate_supplement_tasks(tasks, **kwargs)
    assert [t.novelty_point_id for t in result] == ['NP-1', 'NP-2']
    if kind == 'initial':
        assert result == tasks  # Repeated T-1 across different points is valid.
    else:
        assert all(t.task_id == 'T-R2-COUNT-1' and t.attempt == 2 for t in result)


def test_initial_task_identity_is_checked_before_dispatch():
    tasks, kwargs = inputs('initial')
    tasks[0] = tasks[0].model_copy(update={'attempt': 2})
    with pytest.raises(CodedFailure) as error:
        validate_supplement_tasks(tasks, **kwargs)
    assert error.value.code == C.COORDINATOR_TASK_IDENTITY


def test_valid_production_initial_plan_uses_gate_without_model_call(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    workflow, _ = build_workflow()
    workflow.services = replace(workflow.services,
        coordinator=NoveltyCoordinatorAgent(research_languages=('en',)))
    _, kwargs = inputs('initial')
    state = {'paper': make_paper(), 'novelty_points': kwargs['brief'].novelty_points}
    planned = asyncio.run(workflow._plan(state))
    sends = asyncio.run(workflow._dispatch_planning_tasks({**state, **planned}))
    assert len(sends) == 2
    assert all(s.arg['current_task'].novelty_point_id == s.arg['current_point'].point_id for s in sends)
    assert planned['rounds'] == 1
    assert 'recovery_decisions' not in planned


@pytest.mark.parametrize('case,code', [
    ('unknown_point', C.COORDINATOR_POINT_SCOPE),
    ('changed_points', C.COORDINATOR_TASK_BINDING),
    ('duplicate_task', C.COORDINATOR_TASK_DUPLICATE),
])
def test_invalid_initial_batch_stops_before_any_planner_or_researcher_call(tmp_path, monkeypatch, case, code):
    monkeypatch.chdir(tmp_path)

    class InvalidCoordinator(DemoCoordinator):
        def plan(self, *args, **kwargs):
            brief = super().plan(*args, **kwargs)
            tasks = list(brief.research_tasks)
            if case == 'changed_points':
                return brief.model_copy(update={'novelty_points': [
                    brief.novelty_points[0].model_copy(update={'point_id': 'NP-other'})]})
            if case == 'unknown_point':
                tasks[-1] = tasks[-1].model_copy(update={'novelty_point_id': 'NP-missing'})
            else:
                tasks.append(tasks[0])
            return brief.model_copy(update={'research_tasks': tasks})

    planner = RecordingPlanner()
    workflow, researcher = build_workflow(planner=planner, max_rounds=1)
    workflow.services = replace(workflow.services, coordinator=InvalidCoordinator())
    with pytest.raises(CodedFailure) as error:
        workflow.run(make_paper())
    assert error.value.code == code
    assert planner.calls == researcher.calls == []
    root = next(Path('outputs/paper-test/runtime').iterdir())
    assert json.loads((root / 'manifest.json').read_text())['status'] == 'FAILED'
    errors = [json.loads(p.read_text()) for p in (root / 'errors').glob('*.json')]
    assert any(e.get('code') == code.value for e in errors)


def test_direct_dispatch_rejects_unknown_point_instead_of_silently_skipping():
    workflow, _ = build_workflow()
    tasks, kwargs = inputs('initial')
    tasks[1] = tasks[1].model_copy(update={'novelty_point_id': 'NP-missing'})
    with pytest.raises(CodedFailure) as error:
        asyncio.run(workflow._dispatch_planning_tasks({'paper': make_paper(),
            'novelty_points': kwargs['brief'].novelty_points, 'research_tasks': tasks}))
    assert error.value.code == C.COORDINATOR_POINT_SCOPE
