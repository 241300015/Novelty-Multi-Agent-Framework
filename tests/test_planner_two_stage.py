"""Live contract tests: Harness owns stages and freezes accepted concepts."""
import asyncio
import json
from copy import deepcopy

import pytest
from backend.env import ModelResponse
from novelty_agent_framework.agents import SearchPlannerAgent, SearchPlannerExhaustedError
from novelty_agent_framework.core.planner_harness import AcceptedConceptCatalog
from novelty_agent_framework.schemas.search_plan_draft import SearchConceptsDraft
from test_search_planner import make_point, make_task, valid_draft


def concept_payload():
    return {'concepts': valid_draft(make_task(language='en'))['concepts']}


def strategy_payload():
    return {'strategies': [{'level': level, 'focus_concepts': ['C1', 'C2']}
                           for level in ('strict', 'medium', 'broad')]}


def feedback(messages):
    content = messages[1].content
    return json.JSONDecoder().raw_decode(content[content.index('{"stage":'):])[0]


def repair(fields):
    def respond(messages, options):
        f = feedback(messages)
        return {'option_id': next(iter(f['options'])), 'updates': fields(f)}
    return respond


class Client:
    def __init__(self, *responses):
        self.responses = iter(responses)
        self.calls = []

    def complete(self, messages, *, options=None):
        self.calls.append((messages, options))
        response = next(self.responses)
        if isinstance(response, BaseException):
            raise response
        if callable(response):
            response = response(messages, options)
        return ModelResponse(content=response if isinstance(response, str) else json.dumps(response))

    async def acomplete(self, messages, *, options=None):
        return self.complete(messages, options=options)


def run(client, *, asynchronous=False, **kwargs):
    agent = SearchPlannerAgent(model_client=client, **kwargs)
    args = (make_point(), make_task(language='en'))
    return asyncio.run(agent.aplan(*args)) if asynchronous else agent.plan(*args)


@pytest.mark.parametrize('asynchronous', [False, True])
def test_two_calls_have_disjoint_contracts_and_bound_strategy_references(asynchronous):
    client = Client(concept_payload(), strategy_payload())
    plan = run(client, asynchronous=asynchronous, max_attempts=1)
    assert len(client.calls) == 2
    first, second = [options.response_format['json_schema']['schema'] for _, options in client.calls]
    assert set(first['properties']) == {'concepts'}
    assert set(second['properties']) == {'strategies'}
    assert second['$defs']['SearchStrategyDraft']['properties']['focus_concepts']['items']['enum'] == ['C1','C2','C3']
    assert 'Harness 固定概念目录' in client.calls[1][0][1].content
    assert 'dynamic neighbor sampling' in client.calls[1][0][1].content
    assert [c.terms for c in plan.concepts] == [c['terms'] for c in concept_payload()['concepts']]
    assert all(o.tools == () and o.tool_choice == 'none' for _,o in client.calls)


def test_concept_failure_never_starts_strategy_generation():
    payload = concept_payload()
    payload['concepts'][0]['alias'] = ['中文']
    client = Client(payload)
    with pytest.raises(SearchPlannerExhaustedError) as exc:
        run(client, max_attempts=1)
    assert len(client.calls) == 1
    assert exc.value.planning_stage == 'concepts'
    assert exc.value.audit['catalog_sha256'] is None


def test_corrected_concepts_are_frozen_before_strategy_generation():
    payload = concept_payload()
    payload['concepts'][0]['alias'] = ['中文']
    def updates(f):
        spec = f['fields']['field_0']
        assert spec['concept_context']['terms'] == payload['concepts'][0]['terms']
        assert spec['current_value'] == ['中文']
        return {'field_0': ['DGNN']}
    client = Client(payload, repair(updates), strategy_payload())
    plan = run(client)
    assert len(client.calls) == 3
    assert plan.concepts[0].alias == ['DGNN']
    last = client.calls[-1][0][1].content
    assert '中文' not in last and 'DGNN' in last


@pytest.mark.parametrize('rebuild', [False, True])
def test_strategy_repair_never_reopens_concepts_even_when_reconstructing(rebuild):
    bad = 'not json' if rebuild else {'strategies':[{'level':'strict','focus_concepts':['C999']}]}
    def fix(messages, options):
        f = feedback(messages)
        schema = options.response_format['json_schema']['schema']
        assert 'SearchConceptDraft' not in schema.get('$defs', {})
        if rebuild:
            assert set(schema['properties']['draft']['anyOf'][0]['properties']) == {'strategies'}
            return {'option_id':next(iter(f['options'])), 'draft':strategy_payload()}
        assert all(spec['path'][0] == 'strategies' for spec in f['fields'].values())
        return {'option_id':next(iter(f['options'])), 'updates':{'field_0':strategy_payload()['strategies']}}
    client=Client(concept_payload(), bad, fix)
    plan=run(client)
    assert len(client.calls)==3
    assert plan.concepts[0].terms==concept_payload()['concepts'][0]['terms']


def test_strategy_cannot_return_or_repair_concepts():
    bad={**strategy_payload(), 'concepts':concept_payload()['concepts']}
    def fix(messages, options):
        f=feedback(messages)
        assert f['fields']['field_0']['operation']=='remove'
        assert f['fields']['field_0']['path']==['concepts']
        # Ignoring the remove contract cannot overwrite the accepted catalog.
        return {'option_id':next(iter(f['options'])), 'updates':{'field_0':[]}}
    client=Client(concept_payload(), bad, fix)
    with pytest.raises(SearchPlannerExhaustedError) as exc:
        run(client)
    assert exc.value.planning_stage=='strategies'
    assert exc.value.audit['catalog_sha256']
    assert len(client.calls)==3


def test_unchanged_strategy_repair_uses_temporary_correction_without_reextracting():
    bad={'strategies':[{'level':'strict'}]}
    same=repair(lambda f: {'field_0':bad['strategies']})
    client=Client(concept_payload(), bad, same, same)
    with pytest.raises(SearchPlannerExhaustedError) as exc:
        run(client)
    assert exc.value.code.value=='planner.repair.no_progress'
    assert exc.value.planning_stage=='strategies'
    assert exc.value.attempts==3 and len(client.calls)==4
    assert '未产生任何实际改动' in client.calls[-1][0][1].content


def test_catalog_is_immutable_and_returns_defensive_copies():
    catalog=AcceptedConceptCatalog(SearchConceptsDraft.model_validate(concept_payload()).model_dump_json())
    digest=catalog.digest
    catalog.concepts()[0].terms.append('corruption')
    catalog.view()[0]['alias'].append('corruption')
    assert catalog.digest==digest
    assert 'corruption' not in str(catalog.view())


def test_concurrent_calls_do_not_share_catalogs():
    async def scenario():
        class Concurrent:
            async def acomplete(self, messages, *, options):
                await asyncio.sleep(0)
                second='"point_id": "NP-2"' in messages[1].content
                term='second unique mechanism' if second else 'first unique mechanism'
                if options.response_format['json_schema']['name']=='planner_concepts':
                    payload=concept_payload()
                    payload['concepts'][0]['terms']=[term]
                else:
                    assert term in messages[1].content
                    assert ('first unique mechanism' if second else 'second unique mechanism') not in messages[1].content
                    payload=strategy_payload()
                return ModelResponse(content=json.dumps(payload))
        agent=SearchPlannerAgent(model_client=Concurrent())
        point,task=make_point(),make_task(language='en')
        return await asyncio.gather(agent.aplan(point,task),agent.aplan(
            point.model_copy(update={'point_id':'NP-2'}),task.model_copy(update={'novelty_point_id':'NP-2'})))
    plans=asyncio.run(scenario())
    assert plans[0].concepts[0].terms!=plans[1].concepts[0].terms


@pytest.mark.parametrize('payload', [
    {**concept_payload(), **strategy_payload()},
    {'search_plan': {**concept_payload(), **strategy_payload()}},
    {'option_id': 'advance_to_strategies'},
])
def test_concept_stage_rejects_joint_plan_wrapper_and_model_phase_choice(payload):
    client = Client(payload)
    with pytest.raises(SearchPlannerExhaustedError) as exc:
        run(client, max_attempts=1)
    assert exc.value.planning_stage == 'concepts'
    assert len(client.calls) == 1
    assert exc.value.code.value == 'model.output.schema_invalid'


def test_strategy_reference_repair_keeps_catalog_enum_and_field_scope():
    payload = strategy_payload()
    payload['strategies'][0]['focus_concepts'] = ['C999']
    def fix(messages, options):
        f = feedback(messages)
        field = f['fields']['field_0']
        assert field['path'] == ['strategies', 0, 'focus_concepts']
        assert f['allowed_paths'] == [['strategies', 0, 'focus_concepts']]
        schema = options.response_format['json_schema']['schema']
        update_schema = schema['properties']['updates']['anyOf'][0]['properties']['field_0']
        assert update_schema['items']['enum'] == ['C1', 'C2', 'C3']
        return {'option_id': next(iter(f['options'])), 'updates': {'field_0': ['C1']}}
    client = Client(concept_payload(), payload, fix)
    plan = run(client)
    assert len(client.calls) == 3
    assert plan.strategies[0].expression == 'C1'
    assert [s.level for s in plan.strategies] == ['strict', 'medium', 'broad']


def test_cancel_during_strategy_call_never_restarts_concept_stage():
    async def scenario():
        entered = asyncio.Event()
        class Blocking(Client):
            async def acomplete(self, messages, *, options=None):
                if self.calls:
                    self.calls.append((messages, options))
                    entered.set()
                    await asyncio.Event().wait()
                return self.complete(messages, options=options)
        client = Blocking(concept_payload())
        pending = asyncio.create_task(SearchPlannerAgent(model_client=client).aplan(
            make_point(), make_task(language='en')))
        await asyncio.wait_for(entered.wait(), 3)
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        assert len(client.calls) == 2
        assert client.calls[-1][1].response_format['json_schema']['name'] == 'planner_strategies'
    asyncio.run(scenario())


def test_both_phase_prompts_are_declared_and_preflight_checks_concept_prompt():
    from novelty_agent_framework.config import load_application_config
    from novelty_agent_framework.config.experiment import configured_prompt_names, preflight_config
    config = load_application_config(environ={}, overrides={
        'search_planner': {'concept_prompt': 'search_planner/missing_concepts'}})
    names = configured_prompt_names(config)['search_planner']
    assert names == {'concepts': 'search_planner/missing_concepts', 'strategies': 'search_planner/plan'}
    errors = [issue for issue in preflight_config(config, environ={}) if issue['code'] == 'prompt_missing']
    assert any(issue['path'] == 'search_planner.concepts' for issue in errors)
