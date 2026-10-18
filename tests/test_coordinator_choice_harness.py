"""Exercise choice-only Coordinator execution and the real indexed Reader."""
import asyncio
import json
from pathlib import Path

import pytest
from backend.env import ModelResponse, ModelToolCall
from novelty_agent_framework.core.coordinator_policy import COORDINATOR_WORK_SPECS, work_spec
from novelty_agent_framework.core.tool_call_harness import ToolCallHarnessConfig
from novelty_agent_framework.persistence import persist_workflow_input
from novelty_agent_framework.schemas.failures import FailureCode as C
from novelty_agent_framework.schemas.supplement_planning import SupplementPlanningInput
from test_supplement_coordinator import setup, task


@pytest.fixture(autouse=True)
def isolate(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)


def final(*, reads=(), code='evidence.missing', **updates):
    spec = COORDINATOR_WORK_SPECS[code]
    decision = dict(point_id='NP-1', action=spec.action, reason='LLM chose a different mechanism comparison.',
        basis_read_ids=list(reads), research_tasks=[{**task(), 'task_type': spec.task_type}])
    decision.update(updates)
    return ModelResponse(content=json.dumps({'trigger_code': code, 'decisions': [decision]}))


def menu(messages, options):
    assert options.tools == () and options.tool_choice == 'none'
    assert all(m.role != 'tool' and not m.tool_calls for m in messages)
    choices = json.loads(messages[-1].content.split('\n', 1)[1].split('\nReturn', 1)[0])
    if choices:
        spec = options.response_format['json_schema']
        assert spec['strict'] and spec['schema']['required'] == ['option_id']
        assert spec['schema']['additionalProperties'] is False
        assert spec['schema']['properties']['option_id']['enum'] == list(choices)
    else:
        assert options.response_format == {'type': 'json_object'}
    return choices


def choice(option):
    return ModelResponse(content=json.dumps({'option_id': option}))


def finish_choice(messages, options):
    return choice(next(k for k, v in menu(messages, options).items() if v.startswith('结束')))


def read_choice(messages, options):
    return choice(next(k for k, v in menu(messages, options).items() if ' M1：' in v))


def observations(messages):
    return [json.loads(m.content.split('\n', 1)[1]) for m in messages
        if m.content and m.content.startswith('Harness observation:\n')]


def final_from_read(messages, options):
    assert menu(messages, options) == {}
    read = next(o['read_result'] for o in reversed(observations(messages)) if 'read_result' in o)
    assert read['text'] and read['json_pointer'] == '/full_text'
    return final(reads=[read['read_id']])


class Script:
    def __init__(self, *steps):
        self.steps, self.calls = list(steps), []

    async def acomplete(self, messages, *, options=None):
        self.calls.append((list(messages), options))
        assert options.tools == () and options.tool_choice == 'none'
        step = self.steps.pop(0)
        return step(messages, options) if callable(step) else step


def execute(model, *, kind='count', config=None, full_text=None):
    workflow, state, _ = setup(kind)
    workflow.services.coordinator.model_client = model
    workflow.services.coordinator.supplement_harness_config = config
    if full_text is not None:
        state['paper'] = state['paper'].model_copy(update={'full_text': full_text})
    persist_workflow_input(state['paper'], output_root=workflow.output_root)
    checked = asyncio.run(workflow._check_final_evidence_sufficiency(state))
    result = asyncio.run(workflow._plan_supplement({**state, **checked}))
    audit = json.loads(Path('outputs/paper-test/recovery/run-supplement-test/round-1-coordinator.json').read_text())
    audit['harness'] = audit['harness']['trigger_workflows'][0]
    return result, audit, state


def test_choice_reads_paper_and_binds_llm_decision():
    model = Script(read_choice, finish_choice, final_from_read)
    result, audit, state = execute(model)
    decision = result['recovery_decisions'][0]
    assert decision.reason == 'LLM chose a different mechanism comparison.'
    assert decision.planning_origin == 'coordinator_llm'
    assert len(decision.planning_basis_read_ids) == 1
    assert decision.planned_task_ids == ['T-R2-COUNT-1']
    assert state['paper'].full_text not in model.calls[0][0][1].content
    read = next(o['read_result'] for o in observations(model.calls[1][0]) if 'read_result' in o)
    assert read['text'] == state['paper'].full_text
    harness = audit['harness']
    assert harness['tool_calls_used'] == 1
    assert harness['policy']['mode'] == 'choice_only' and harness['policy']['stage'] == 'draft'
    calls = [e['tool_call'] for e in harness['trace'] if e['kind'] == 'tool_call']
    assert calls[0]['arguments'] == dict(material_id='M1', content_pointer='/full_text', char_start=0, max_chars=4000)
    assert decision.reason in result['research_tasks'][0].description


@pytest.mark.parametrize('kind,code', [('count', 'evidence.missing'),
    ('semantic', 'review.missing_features'), ('zero', 'coverage.no_match')])
def test_trigger_selects_executable_spec_not_a_prompt(kind, code):
    model = Script(finish_choice, final(code=code))
    result, audit, _ = execute(model, kind=kind)
    spec = COORDINATOR_WORK_SPECS[code]
    assert result['research_tasks'][0].task_type == spec.task_type
    records = audit['harness']['guidance']
    assert len(records) == 1 and records[0]['trigger_code'] == code
    assert records[0]['specification']['name'] == spec.name
    assert records[0]['guidance_id'].startswith(f'coordinator.policy/{spec.name}@1:')
    assert 'prompt_name' not in records[0]
    assert 'coordinator/recovery/' not in model.calls[0][0][0].content
    assert audit['harness']['tool_calls_used'] == 0


@pytest.mark.parametrize('bad,code', [
    (choice('M999'), C.HARNESS_OPTION_UNKNOWN),
    (ModelResponse(content='{"option_id": "v1_0", "path": "/etc/passwd"}'), C.HARNESS_DECISION_SCHEMA),
    (ModelResponse(content='{"option_id": null}'), C.HARNESS_DECISION_SCHEMA),
    (ModelResponse(content='not JSON'), C.HARNESS_DECISION_SCHEMA),
    (ModelResponse(content='', tool_calls=(ModelToolCall(id='bad', name='reader', arguments={'material_id': 'M1'}),)), C.HARNESS_NATIVE_CALL),
    (ModelResponse(content='', tool_calls=(ModelToolCall(id='bad', name='database_search', arguments={}),)), C.HARNESS_NATIVE_CALL),
    (final(), C.HARNESS_EARLY_FINISH),
])
def test_invalid_selection_has_no_io_and_can_be_corrected(bad, code):
    result, audit, _ = execute(Script(bad, read_choice, finish_choice, final_from_read))
    assert result['research_tasks'] and result['rounds'] == 2
    assert code in {f.code for f in result['failure_events']}
    assert audit['harness']['tool_calls_used'] == 1


@pytest.mark.parametrize('use_label', [False, True])
def test_stale_ids_and_display_labels_are_rejected(use_label):
    issued = []
    def read(messages, options):
        selected = read_choice(messages, options)
        issued.append(json.loads(selected.content)['option_id'])
        return selected
    def invalid(messages, options):
        choices = menu(messages, options)
        return choice(next(iter(choices.values())) if use_label else issued[0])
    result, audit, _ = execute(Script(read, invalid, finish_choice, final_from_read))
    assert (C.HARNESS_OPTION_LABEL if use_label else C.HARNESS_OPTION_STALE) in {f.code for f in result['failure_events']}
    assert result['research_tasks'] and audit['harness']['tool_calls_used'] == 1


def test_pagination_uses_observed_end_then_removes_eof_option():
    def after_first(messages, options):
        label = next(v for v in menu(messages, options).values() if ' M1：' in v)
        assert '继续读取' in label and '4000' in label
        return read_choice(messages, options)
    def after_eof(messages, options):
        assert all(' M1：' not in v for v in menu(messages, options).values())
        return finish_choice(messages, options)
    result, audit, _ = execute(Script(read_choice, after_first, after_eof, final_from_read), full_text='x' * 5000)
    reads = [e['observation']['payload']['read_result'] for e in audit['harness']['trace'] if e.get('observation')]
    assert [(r['char_start'], r['char_end']) for r in reads] == [(0, 4000), (4000, 5000)]
    assert result['research_tasks'] and audit['harness']['policy']['read_chars_used'] == 5000


def test_changed_material_is_refused_and_removed_from_menu():
    def change_file(messages, options):
        Path('outputs/paper-test/paper-input/others/paper.json').write_text('{"full_text": "tampered content"}')
        return read_choice(messages, options)
    def stop(messages, options):
        assert any(C.READER_HASH_MISMATCH.value in m.content for m in messages if m.content)
        assert all('tampered content' not in m.content for m in messages if m.content)
        assert all(' M1：' not in v for v in menu(messages, options).values())
        return finish_choice(messages, options)
    result, _, _ = execute(Script(change_file, stop, final()))
    assert result['research_tasks']
    assert C.READER_HASH_MISMATCH in {f.code for f in result['failure_events']}
    assert result['recovery_decisions'][0].trigger_codes == ['evidence.missing']


@pytest.mark.parametrize('output,expected_code', [
    (final(reads=['invented-read-id']), C.MODEL_OUTPUT_SCHEMA),
    (final(action='replan_query'), C.MODEL_OUTPUT_SCHEMA),
    (final(point_id='NP-99'), C.MODEL_OUTPUT_SCHEMA),
    (final(trigger_codes=['review.missing_features']), C.MODEL_SCHEMA_INVALID),
    (ModelResponse(content=json.dumps([task()])), C.MODEL_SCHEMA_INVALID),
    (final(research_tasks=[{**task(), 'task_type': 'feature_supplement'}]), C.COORDINATOR_TASK_TYPE),
    (final(research_tasks=[task('NP-99')]), C.COORDINATOR_TASK_BINDING),
])
def test_invalid_final_decisions_stop_without_task_fallback(output, expected_code):
    result, audit, _ = execute(Script(finish_choice, output))
    assert result['research_tasks'] == [] and 'rounds' not in result
    assert result['recovery_decisions'][0].action.value == 'stop'
    assert audit['harness']['failure_code'] == expected_code.value
    assert expected_code in {f.code for f in result['failure_events']}


def test_budget_forces_one_tool_free_finalization_and_restores_draft_schema():
    config = ToolCallHarnessConfig(max_turns=2, max_tool_calls=1, per_tool_limits={'reader': 1},
        max_total_read_chars=1000, reserve_final_turn=True, finalize_on_budget=True,
        finalization_instruction='Output CoordinatorRecoveryDraft, no more tools.')
    def finish(messages, options):
        assert options.tools == () and options.tool_choice == 'none'
        assert options.response_format == {'type': 'json_object'}
        reads = [o['read_result'] for o in observations(messages) if 'read_result' in o]
        return final(reads=[reads[0]['read_id']])
    model = Script(read_choice, finish)
    result, audit, _ = execute(model, config=config)
    assert result['research_tasks'] and len(model.calls) == 2
    assert audit['harness']['policy']['forced_finalization_reason']


def test_runtime_clamps_read_arguments_to_remaining_character_budget():
    config = ToolCallHarnessConfig(max_turns=4, max_tool_calls=3, max_total_read_chars=8)
    def stop(messages, options):
        choices = menu(messages, options)
        assert len(choices) == 1 and next(iter(choices.values())).startswith('结束')
        return finish_choice(messages, options)
    result, audit, _ = execute(Script(read_choice, stop, final_from_read), config=config, full_text='x' * 20)
    assert result['research_tasks']
    calls = [e['tool_call'] for e in audit['harness']['trace'] if e['kind'] == 'tool_call']
    assert calls[0]['arguments']['max_chars'] == 8
    assert audit['harness']['policy']['read_chars_used'] == 8


def test_missing_shared_prompt_fails_before_any_model_call(tmp_path):
    from backend.env import PromptLibrary
    workflow, state, _ = setup('count')
    model = Script(final())
    workflow.services.coordinator.model_client = model
    workflow.services.coordinator._prompts = PromptLibrary(tmp_path / 'missing-prompts')
    checked = asyncio.run(workflow._check_final_evidence_sufficiency(state))
    result = asyncio.run(workflow._plan_supplement({**state, **checked}))
    assert not model.calls and not result['research_tasks']


def test_multi_trigger_input_selects_one_executable_spec_each():
    from novelty_agent_framework.core.coordinator_harness import trigger_inputs
    execute(Script(finish_choice, final()))
    path = Path('outputs/paper-test/recovery/run-supplement-test/attempt-2-input.json')
    request = SupplementPlanningInput.model_validate_json(path.read_text())
    base = request.recovery_decisions[0]
    request = SupplementPlanningInput.model_validate({**request.model_dump(), 'recovery_decisions': [
        {**base.model_dump(), 'trigger_codes': [code], 'action': spec.action}
        for code, spec in COORDINATOR_WORK_SPECS.items()]})
    groups = list(trigger_inputs(request))
    assert len(groups) == 3
    for code, scoped in groups:
        assert work_spec(scoped) == (code, COORDINATOR_WORK_SPECS[code])


def test_same_trigger_covers_two_points_in_one_workflow():
    workflow, state, _ = setup('count')
    point = state['brief'].novelty_points[0]
    points = [point, point.model_copy(update={'point_id': 'NP-2'})]
    state['brief'] = workflow.services.coordinator.plan(state['paper'], points=points, attempt=1)
    state['novelty_points'] = points
    state['research_tasks'] = state['all_research_tasks'] = state['brief'].research_tasks
    payload = json.loads(final().content)
    payload['decisions'].append({**payload['decisions'][0], 'point_id': 'NP-2', 'research_tasks': [task('NP-2')]})
    model = Script(finish_choice, ModelResponse(content=json.dumps(payload)))
    workflow.services.coordinator.model_client = model
    checked = asyncio.run(workflow._check_final_evidence_sufficiency(state))
    result = asyncio.run(workflow._plan_supplement({**state, **checked}))
    assert len(model.calls) == 2
    assert {d.point_id for d in result['recovery_decisions']} == {'NP-1', 'NP-2'}
    assert len(result['research_tasks']) == 2
    assert all(d.coordinator_status == 'planned' for d in result['recovery_decisions'])


def test_three_codes_remain_independent_and_cannot_share_read_ids():
    from novelty_agent_framework.schemas import NoveltyPointReview
    workflow, state, _ = setup('zero')
    state['evidence_cards'] = []
    state['novelty_reviews'] = [NoveltyPointReview(novelty_point_id='NP-1',
        status='insufficient_evidence', incomplete_reason='semantic_evidence')]
    persist_workflow_input(state['paper'])
    previous_reads = []
    def count_finish(messages, options):
        read_id = next(o['read_result']['read_id'] for o in observations(messages) if 'read_result' in o)
        previous_reads.append(read_id)
        return final(reads=[read_id])
    def invalid_semantic_finish(messages, options):
        assert previous_reads[0] not in ''.join(m.content or '' for m in messages)
        return final(code='review.missing_features', reads=previous_reads)
    model = Script(read_choice, finish_choice, count_finish, finish_choice, invalid_semantic_finish,
                   finish_choice, final(code='coverage.no_match'))
    workflow.services.coordinator.model_client = model
    checked = asyncio.run(workflow._check_final_evidence_sufficiency(state))
    result = asyncio.run(workflow._plan_supplement({**state, **checked}))
    decisions = {d.trigger_codes[0]: d for d in result['recovery_decisions']}
    assert decisions['evidence.missing'].coordinator_status == 'planned'
    assert decisions['review.missing_features'].coordinator_status == 'failed'
    assert decisions['coverage.no_match'].coordinator_status == 'planned'
    assert {t.task_type for t in result['research_tasks']} == {'research_gap', 'replan_query'}
    assert len({t.task_id for t in result['research_tasks']}) == 2
    assert result['rounds'] == 2 and len(model.calls) == 7
    for task_value in result['research_tasks']:
        directive = result['recovery_directives'][json.dumps(['NP-1', task_value.task_id])]
        assert task_value.task_id in directive.planned_task_ids
    audit = json.loads(Path('outputs/paper-test/recovery/run-supplement-test/round-1-coordinator.json').read_text())
    groups = audit['harness']['trigger_workflows']
    assert len(groups) == 3
    for group in groups:
        assert len(group['guidance']) == 1 and group['policy']['mode'] == 'choice_only'
        assert all(r['trigger_code'] == group['trigger_code'] for r in group['input']['material_index']
                   if r['kind'] == 'recovery_decision')
