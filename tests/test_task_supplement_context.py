"""Task context survives execution and persistence without entering public prose."""
import asyncio
from copy import deepcopy
import json

import pytest
from pydantic import ValidationError

from novelty_agent_framework.core.recovery_planning import recovery_attempts
from novelty_agent_framework.persistence import persist_retrieval_plans
from novelty_agent_framework.schemas import PaperInput, ResearchTask
from novelty_agent_framework.tools import ResearcherToolRegistry
from novelty_agent_framework.tools.renderer import render_report
from novelty_agent_framework.workflows import TaskResearcherWorkflow
from test_renderer import seed_workspace, refresh_fixture_versions
from test_task_researcher_workflow import FakeModel, FakeBuilder, finish, scope


CONTEXT = {'decision': {'reason': 'INTERNAL_SENTINEL'}, 'count_gaps': [], 'previous_attempts': []}


def task_payload(**updates):
    return dict(task_id='T1', novelty_point_id='NP-1', task_type='research_gap',
                language='en', description='补充方法证据', attempt=2, **updates)


@pytest.mark.parametrize('raw', [json.dumps(CONTEXT), '{incomplete json', '[1, 2]', ''])
def test_legacy_context_is_read_without_losing_payload_or_mutating_input(raw):
    payload = task_payload()
    payload['description'] += '\n补检系统上下文：' + raw
    original = deepcopy(payload)
    task = ResearchTask.model_validate(payload)
    expected = CONTEXT if raw == json.dumps(CONTEXT) else {'legacy_raw_context': raw}
    assert task.description == '补充方法证据'
    assert task.supplement_context == expected
    assert ResearchTask.model_validate_json(task.model_dump_json()) == task
    assert recovery_attempts([task], [], run_id='r')[0].description == task.description
    assert payload == original


def test_dual_context_does_not_silently_overwrite_conflicting_facts():
    payload = task_payload(supplement_context=CONTEXT)
    payload['description'] += '\n补检系统上下文：' + json.dumps(CONTEXT)
    assert ResearchTask.model_validate(payload).supplement_context == CONTEXT
    payload['supplement_context'] = {'different': 'facts'}
    with pytest.raises(ValidationError, match='conflicting'):
        ResearchTask.model_validate(payload)


def test_context_roundtrips_through_persistence_and_reaches_researcher(tmp_path):
    request = scope()
    task = request.research_task.model_copy(update={
        'description': '补充方法证据', 'supplement_context': deepcopy(CONTEXT)})
    paper = PaperInput(paper_id='paper-1', title='Test', full_text='body')
    path = persist_retrieval_plans(paper, [task], search_plans=[request.search_plan],
                                 rounds=2, output_root=tmp_path)
    saved = json.loads(path.read_text())['novelty_point_plans'][0]['research_tasks'][0]
    assert saved['description'] == task.description
    assert saved['supplement_context'] == CONTEXT
    restored = ResearchTask.model_validate(saved)
    from novelty_agent_framework.agents import SearchPlannerAgent
    from test_search_planner import StubModelClient, valid_draft
    planner_model = StubModelClient(json.dumps(valid_draft(restored)))
    SearchPlannerAgent(model_client=planner_model).plan(request.novelty_point, restored)
    planner_prompt = planner_model.calls[0][0][1].content
    assert 'supplement_context' in planner_prompt and 'INTERNAL_SENTINEL' in planner_prompt
    model = FakeModel([finish()])
    result = asyncio.run(TaskResearcherWorkflow(model, ResearcherToolRegistry(), FakeBuilder()).ainvoke(
        request.model_copy(update={'research_task': restored})))
    assert result.status.value == 'completed'
    prompt = model.messages[0][1].content
    assert 'supplement_context' in prompt and 'INTERNAL_SENTINEL' in prompt
    assert '补检系统上下文：' not in prompt


@pytest.mark.parametrize('legacy', [False, True])
def test_both_reports_hide_internal_context_and_leave_sealed_inputs_unchanged(tmp_path, legacy):
    seed_workspace(tmp_path)
    workspace = tmp_path / 'renderer-paper'
    path = workspace / 'retrieval-plans.json'
    plans = json.loads(path.read_text())
    task = plans['novelty_point_plans'][0]['research_tasks'][0]
    task['description'] = '补充方法证据'
    if legacy:
        task['description'] += '\n补检系统上下文：' + json.dumps(CONTEXT)
    else:
        task['supplement_context'] = CONTEXT
    path.write_text(json.dumps(plans))
    refresh_fixture_versions(workspace, 'retrieval-plans.json', 'evidence-cards.json')
    before = {p: p.read_bytes() for p in (path, workspace/'report-inputs.json', workspace/'report.json')}
    for name in ('default', 'concise'):
        text = render_report(paper_name='renderer-paper', output_root=tmp_path, template_name=name).read_text()
        assert 'INTERNAL_SENTINEL' not in text and '补检系统上下文' not in text
        assert 'supplement_context' not in text
        if name == 'default':
            assert '补充方法证据' in text
    assert before == {p: p.read_bytes() for p in before}
