"""One bounded scope correction; only runtime-issued field slots can be applied."""
import asyncio
from copy import deepcopy
import json

import pytest

from novelty_agent_framework.agents import SearchPlannerAgent, SearchPlannerExhaustedError
from novelty_agent_framework.agents.search_plan_compiler import SemanticLimits
from novelty_agent_framework.core import RuntimeArtifactManager, RuntimeDebugConfig
from novelty_agent_framework.core.planner_repair import PlannerRepairHarness
from novelty_agent_framework.schemas.failures import CodedFailure, FailureCode as C
from test_planner_recovery import Client, http_error, run
from test_planner_repair_harness import importance_error
from test_search_planner import make_point, make_task, repair_reply, valid_draft


def rejected_draft():
    draft = valid_draft(make_task())
    draft['concepts'][1]['terms'] = ['unauthorized term']
    return draft


def updates_reply(updates):
    def reply(options):
        schema = options.response_format['json_schema']['schema']
        assert options.response_format['json_schema']['name'] == 'planner_scope_correction'
        assert schema['properties']['updates']['anyOf'][0]['additionalProperties'] is False
        option = schema['properties']['option_id']['enum'][0]
        return json.dumps({'option_id': option, 'updates': updates})
    return reply


def client_with_correction(reply):
    return Client(json.dumps(importance_error()), repair_reply(rejected_draft()), reply)


@pytest.mark.parametrize('asynchronous', [False, True])
def test_scope_violation_gets_one_constrained_correction_from_original(asynchronous):
    client = client_with_correction(updates_reply({'field_0': 2}))
    plan = run(client, asynchronous)
    assert len(client.calls) == 3
    assert plan.concepts[0].importance == 2
    assert plan.concepts[1].terms == valid_draft(make_task())['concepts'][1]['terms']
    schema = client.calls[2][1].response_format['json_schema']['schema']
    slots = schema['properties']['updates']['anyOf'][0]['properties']
    assert set(slots) == {'field_0'}
    assert slots['field_0']['maximum'] == 3
    assert '"rejected_paths": [["concepts", 1, "terms", 0]]' in client.calls[2][0][1].content
    assert '"protocol": "field_updates"' in client.calls[2][0][1].content
    assert '禁止返回完整 draft' in client.calls[2][0][0].content
    assert client.calls[1][1].response_format != client.calls[2][1].response_format


@pytest.mark.parametrize('reply,code', [
    (updates_reply({'other_field': 1}), C.HARNESS_DECISION_SCHEMA),
    (updates_reply({'concepts': []}), C.HARNESS_DECISION_SCHEMA),
    (repair_reply(valid_draft(make_task())), C.HARNESS_DECISION_SCHEMA),
    (updates_reply({'field_0': 9}), C.MODEL_SCHEMA_INVALID),
    (updates_reply({'field_0': {'path': ['concepts', 1, 'terms'], 'value': ['forged']}}), C.MODEL_SCHEMA_INVALID),
    (updates_reply({}), C.MODEL_SCHEMA_INVALID),
    ('broken JSON', C.MODEL_JSON_INVALID),
])
def test_bad_protocol_correction_never_starts_another_repair(reply, code):
    client = client_with_correction(reply)
    with pytest.raises(SearchPlannerExhaustedError) as caught:
        run(client, True, max_attempts=10)
    assert caught.value.code == code and len(client.calls) == 3
    assert caught.value.history[1]['action'] == 'restrict_repair'
    assert caught.value.history[-1]['repair']['protocol_corrections'] == 1


def test_new_error_code_after_assembly_does_not_reset_semantic_repair_budget():
    broken = valid_draft(make_task())
    broken['concepts'][0]['terms'] = ['']
    rejected = rejected_draft()
    client = Client(json.dumps(broken), repair_reply(rejected), updates_reply({'field_0': ['efficient']}))
    with pytest.raises(SearchPlannerExhaustedError) as caught:
        run(client, False, max_attempts=10)
    assert len(client.calls) == 3
    assert caught.value.code == C.PLANNER_GENERIC_TERM
    assert caught.value.terminal_reason == 'repair_budget_exhausted'
    assert caught.value.history[-1]['repair']['revalidation'] == 'failed'


def test_transport_retry_preserves_the_restricted_request(monkeypatch):
    async def sleep(delay):
        pass
    monkeypatch.setattr('novelty_agent_framework.agents.search_planner.asyncio.sleep', sleep)
    client = Client(json.dumps(importance_error()), repair_reply(rejected_draft()),
                    http_error(503), updates_reply({'field_0': 2}))
    assert run(client, True, max_attempts=4).concepts[0].importance == 2
    assert len(client.calls) == 4
    assert client.calls[2] == client.calls[3]


def test_default_total_budget_does_not_expand_for_protocol_transport_failure():
    client = Client(json.dumps(importance_error()), repair_reply(rejected_draft()), http_error(503))
    with pytest.raises(SearchPlannerExhaustedError) as caught:
        run(client, False)
    assert caught.value.code == C.MODEL_HTTP_SERVER
    assert caught.value.terminal_reason == 'attempt_limit' and len(client.calls) == 3


def test_schema_extra_field_removal_is_a_program_selected_operation():
    broken = valid_draft(make_task())
    broken['task_id'] = 'forged'
    client = Client(json.dumps(broken), repair_reply(rejected_draft()), updates_reply({'field_0': None}))
    plan = run(client, False)
    assert plan.task_id == 'T-1'
    assert plan.concepts[1].terms == broken['concepts'][1]['terms']
    assert '"operation": "remove"' in client.calls[2][0][1].content


def test_array_element_correction_preserves_other_elements():
    broken = valid_draft(make_task())
    broken['concepts'][0]['terms'] = [None, 'valid graph term']
    rejected = deepcopy(broken)
    rejected['concepts'][0]['terms'][0] = 'valid replacement'
    rejected['concepts'][0]['importance'] = 2
    client = Client(json.dumps(broken), repair_reply(rejected), updates_reply({'field_0': 'graph summary'}))
    plan = run(client, True)
    assert plan.concepts[0].terms == ['graph summary', 'valid graph term']
    assert plan.concepts[0].importance == 3


def test_structural_scope_collapses_overlapping_paths_and_keeps_linked_references():
    broken = valid_draft(make_task())
    broken['concepts'].append({'terms': ['efficient']})
    broken['strategies'][0]['focus_concepts'] = ['C4']
    rejected = valid_draft(make_task())
    rejected['strategies'][0]['level'], rejected['strategies'][1]['level'] = 'medium', 'strict'
    client = Client(json.dumps(broken), repair_reply(rejected), updates_reply({
        'field_1': ['C1', 'C2'], 'field_0': valid_draft(make_task())['concepts'],
    }))
    plan = run(client, False, semantic_limits=SemanticLimits(max_concepts=3))
    assert len(plan.concepts) == 3
    assert [s.level for s in plan.strategies] == ['strict', 'medium', 'broad']
    slots = client.calls[2][1].response_format['json_schema']['schema']['properties']['updates']['anyOf'][0]['properties']
    assert len(slots) == 4  # Concept collection + three strategy reference fields, no overlapping term slot.


def restricted_harness():
    harness = PlannerRepairHarness.prepare(output=importance_error(), issues=[{
        'code': C.MODEL_SCHEMA_INVALID.value, 'path': ['concepts', 0, 'importance'],
    }], task_key='T1')
    old = harness.repair_id
    with pytest.raises(CodedFailure):
        harness.accept({'option_id': old, 'draft': rejected_draft()})
    harness.restrict()
    return harness, old


def test_old_option_cannot_be_replayed_and_original_data_is_not_mutated():
    harness, old = restricted_harness()
    with pytest.raises(CodedFailure) as caught:
        harness.accept({'option_id': old, 'updates': {'field_0': 2}})
    assert caught.value.code == C.HARNESS_OPTION_STALE
    assert harness.output == importance_error()
    with pytest.raises(RuntimeError):
        harness.restrict()


def test_assembled_draft_does_not_share_mutable_data_with_base_or_response():
    harness, _ = restricted_harness()
    candidate = harness.accept({'option_id': harness.repair_id, 'updates': {'field_0': 2}})
    candidate['concepts'][1]['terms'].append('later mutation')
    assert harness.output == importance_error()


def test_cancel_during_protocol_correction_does_not_restart():
    async def scenario():
        entered = asyncio.Event()
        class BlockingClient(Client):
            async def acomplete(self, messages, *, options=None):
                if len(self.calls) == 2:
                    self.calls.append((messages, options))
                    entered.set()
                    await asyncio.Event().wait()
                return self.complete(messages, options=options)
        client = BlockingClient(json.dumps(importance_error()), repair_reply(rejected_draft()))
        pending = asyncio.create_task(SearchPlannerAgent(model_client=client).aplan(make_point(), make_task()))
        await asyncio.wait_for(entered.wait(), 3)
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        assert len(client.calls) == 3
    asyncio.run(scenario())


def test_audit_retains_rejection_protocol_change_and_revalidation(tmp_path):
    manager = RuntimeArtifactManager('scope-correction', config=RuntimeDebugConfig(
        output_root=tmp_path / 'outputs', archive_root=tmp_path / 'archive'))
    manager.activate()
    try:
        run(client_with_correction(updates_reply({'field_0': 2})), False)
    finally:
        manager.deactivate()
    events = [json.loads(p.read_text()) for p in sorted((manager.run_dir / 'planner_events').glob('*.json'))]
    by_step = {e['step']: e['payload'] for e in events}
    rejected = by_step['repair_rejected']
    assert rejected['code'] == C.PLANNER_REPAIR_SCOPE and rejected['action'] == 'restrict_repair'
    assert rejected['previous_output']['draft']['concepts'][1]['terms'] == ['unauthorized term']
    assert by_step['repair_protocol_restricted']['previous_output'] == importance_error()
    assert by_step['repair_revalidated']['revalidation'] == 'passed'
    assert by_step['repair_revalidated']['changed_paths'] == [['concepts', 0, 'importance']]
