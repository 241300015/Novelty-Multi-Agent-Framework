"""The repair contract controls edits; valid drafts cannot bypass that boundary."""
from copy import deepcopy
import json

import pytest

from novelty_agent_framework.agents import SearchPlannerAgent, SearchPlannerExhaustedError
from novelty_agent_framework.agents.search_plan_compiler import (
    DraftIssue, SearchPlanCompilationError, SemanticLimits,
)
from novelty_agent_framework.core import RuntimeArtifactManager, RuntimeDebugConfig
from novelty_agent_framework.core.planner_repair import PlannerRepairHarness
from novelty_agent_framework.schemas.failures import CodedFailure, FailureCode as C
from novelty_agent_framework.schemas.search_plan_draft import SearchPlanDraft
from test_planner_recovery import Client, run
from test_search_planner import make_point, make_task, repair_reply, valid_draft


def importance_error():
    draft = valid_draft(make_task())
    draft['concepts'][0]['importance'] = 9
    return draft


@pytest.mark.parametrize('asynchronous', [False, True])
@pytest.mark.parametrize('change', ['terms', 'reorder', 'strategy', 'identity'])
def test_valid_repair_cannot_change_unrelated_fields(asynchronous, change):
    fixed = valid_draft(make_task())
    if change == 'terms':
        fixed['concepts'][1]['terms'] = ['another valid technical term']
    elif change == 'reorder':
        fixed['concepts'][1:] = reversed(fixed['concepts'][1:])
    elif change == 'strategy':
        fixed['strategies'][0]['focus_concepts'] = ['C1']
    else:
        fixed['task_id'] = 'T-forged'
    client = Client(json.dumps(importance_error()), repair_reply(fixed))
    with pytest.raises(SearchPlannerExhaustedError) as caught:
        run(client, asynchronous, max_attempts=10)
    error = caught.value
    assert error.code == C.PLANNER_REPAIR_SCOPE and len(client.calls) == 2
    assert error.history[-1]['node'] == 'repair_admission'
    assert error.history[-1]['repair']['revalidation'] == 'not_run'
    assert error.history[0]['code'] == C.MODEL_SCHEMA_INVALID.value


def test_omitted_defaults_do_not_count_as_unrelated_edits():
    fixed = SearchPlanDraft.model_validate(valid_draft(make_task())).model_dump()
    client = Client(json.dumps(importance_error()), repair_reply(fixed))
    assert run(client, False).task_id == 'T-1'


def test_extra_mechanical_field_can_be_removed_but_identity_is_compiled():
    broken = valid_draft(make_task())
    broken['task_id'] = 'T-forged'
    client = Client(json.dumps(broken), repair_reply(valid_draft(make_task())))
    assert run(client, False).task_id == 'T-1'


def test_every_occurrence_of_conflicting_exclude_has_its_own_repair_scope():
    broken = valid_draft(make_task())
    broken['concepts'][0]['terms'] = ['graph neural network']
    for concept in broken['concepts'][1:]:
        concept['exclude'] = ['graph neural network']
    fixed = deepcopy(broken)
    for concept in fixed['concepts'][1:]:
        concept['exclude'] = []
    client = Client(json.dumps(broken), repair_reply(fixed))
    plan = run(client, True)
    assert plan.concepts[0].terms == ['graph neural network']
    feedback = client.calls[1][0][1].content
    assert '["concepts", 1, "exclude"]' in feedback
    assert '["concepts", 2, "exclude"]' in feedback
    assert all(not c.exclude for c in plan.concepts)


def test_reference_fix_cannot_change_strategy_level():
    broken = valid_draft(make_task())
    broken['strategies'][0]['focus_concepts'] = ['C999']
    fixed = deepcopy(broken)
    fixed['strategies'][0]['focus_concepts'] = ['C1']
    assert run(Client(json.dumps(broken), repair_reply(fixed)), False).task_id == 'T-1'
    fixed['strategies'][0]['level'], fixed['strategies'][1]['level'] = 'medium', 'strict'
    with pytest.raises(SearchPlannerExhaustedError) as caught:
        run(Client(json.dumps(broken), repair_reply(fixed)), False)
    assert caught.value.code == C.PLANNER_REPAIR_SCOPE


def test_concept_membership_repair_allows_linked_references_but_not_strategy_levels():
    broken = valid_draft(make_task())
    broken['concepts'].insert(0, {'role': 'setting', 'terms': ['distributed setting']})
    broken['strategies'][0]['focus_concepts'] = ['C2', 'C3']
    fixed = valid_draft(make_task())
    fixed['strategies'][0]['focus_concepts'] = ['C1', 'C2']
    limits = SemanticLimits(max_concepts=3)
    assert run(Client(json.dumps(broken), repair_reply(fixed)), False, semantic_limits=limits)
    fixed['strategies'][0]['level'], fixed['strategies'][1]['level'] = 'medium', 'strict'
    with pytest.raises(SearchPlannerExhaustedError) as caught:
        run(Client(json.dumps(broken), repair_reply(fixed)), False, semantic_limits=limits)
    assert caught.value.code == C.PLANNER_REPAIR_SCOPE


@pytest.mark.parametrize('response,code', [
    (lambda options: json.dumps(valid_draft(make_task())), C.HARNESS_DECISION_SCHEMA),
    (lambda options: json.dumps({'option_id': 'invented', 'draft': valid_draft(make_task())}), C.HARNESS_OPTION_UNKNOWN),
    (lambda options: json.dumps({'option_id': [], 'draft': None}), C.HARNESS_DECISION_SCHEMA),
    (lambda options: repair_reply({'search_plan': valid_draft(make_task())})(options), C.HARNESS_DECISION_SCHEMA),
])
def test_repair_protocol_rejects_unissued_or_missing_choices(response, code):
    client = Client(json.dumps(importance_error()), response)
    with pytest.raises(SearchPlannerExhaustedError) as caught:
        run(client, False)
    assert caught.value.code == code and len(client.calls) == 2


def test_stop_choice_is_terminal_and_logged():
    def stop(options):
        option = options.response_format['json_schema']['schema']['properties']['option_id']['enum'][1]
        return json.dumps({'option_id': option, 'draft': None})
    client = Client(json.dumps(importance_error()), stop)
    with pytest.raises(SearchPlannerExhaustedError) as caught:
        run(client, False)
    assert caught.value.code == C.PLANNER_REPAIR_DECLINED
    assert caught.value.history[-1]['repair']['option_id'].startswith('stop_')
    assert caught.value.history[-1]['repair']['revalidation'] == 'not_run'


def test_successful_scope_check_does_not_replace_revalidation_or_reset_budget():
    broken = valid_draft(make_task())
    broken['concepts'][0]['terms'] = ['']
    fixed = deepcopy(broken)
    fixed['concepts'][0]['terms'] = ['efficient']
    client = Client(json.dumps(broken), repair_reply(fixed))
    with pytest.raises(SearchPlannerExhaustedError) as caught:
        run(client, True, max_attempts=10)
    error = caught.value
    assert error.code == C.PLANNER_GENERIC_TERM
    assert error.history[0]['code'] == C.PLANNER_BLANK_TERM.value
    assert error.history[-1]['repair']['revalidation'] == 'failed'
    assert error.terminal_reason == 'repair_budget_exhausted' and len(client.calls) == 2


def test_known_code_without_structured_location_cannot_authorize_rewrite(monkeypatch):
    def compile(*args, **kwargs):
        raise SearchPlanCompilationError('missing location', [DraftIssue('generic_term', 'bad', 'fix')])
    monkeypatch.setattr('novelty_agent_framework.agents.search_planner.build_runtime_plan', compile)
    client = Client(json.dumps(valid_draft(make_task())))
    with pytest.raises(SearchPlannerExhaustedError) as caught:
        run(client, False)
    assert caught.value.code == C.PLANNER_GENERIC_TERM and len(client.calls) == 1
    assert PlannerRepairHarness.prepare(output={}, issues=[{'code': 'unknown'}], task_key='T') is None


def test_choice_is_bound_to_task_and_consumed_once():
    def harness(key):
        return PlannerRepairHarness.prepare(output=importance_error(), issues=[{
            'code': C.MODEL_SCHEMA_INVALID.value, 'path': ['concepts', 0, 'importance'],
        }], task_key=key)
    first, second = harness('T1'), harness('T2')
    response = {'option_id': first.repair_id, 'draft': valid_draft(make_task())}
    with pytest.raises(CodedFailure) as caught:
        second.accept(response)
    assert caught.value.code == C.HARNESS_OPTION_UNKNOWN
    first.accept(response)
    with pytest.raises(CodedFailure) as caught:
        first.accept(response)
    assert caught.value.code == C.HARNESS_OPTION_STALE


def test_repair_audit_links_issue_action_edit_and_revalidation(tmp_path):
    manager = RuntimeArtifactManager('repair-audit', config=RuntimeDebugConfig(
        output_root=tmp_path / 'outputs', archive_root=tmp_path / 'archive'))
    manager.activate()
    try:
        client = Client(json.dumps(importance_error()), repair_reply(valid_draft(make_task())))
        SearchPlannerAgent(model_client=client).plan(make_point(), make_task())
    finally:
        manager.deactivate()
    events = [json.loads(p.read_text()) for p in sorted((manager.run_dir / 'planner_events').glob('*.json'))]
    by_step = {e['step']: e['payload'] for e in events}
    assert by_step['repair_prepared']['issues'][0]['code'] == C.MODEL_SCHEMA_INVALID.value
    assert by_step['repair_scope_checked']['changed_paths'] == [['concepts', 0, 'importance']]
    assert by_step['repair_revalidated']['stage'] == 'succeeded'
    assert by_step['repair_revalidated']['revalidation'] == 'passed'
    assert len(client.calls) == 2
    assert client.calls[1][1].tools == () and client.calls[1][1].tool_choice == 'none'
