"""Typed local correction, terminal failures and asynchronous isolation."""
import asyncio
import json
import urllib.error
from email.message import Message

import pytest

from backend.env import ModelClientError, ModelResponse, ModelToolCall, PromptLibrary
from backend.env.model_client import ModelCallBudgetExceeded, ModelContextAdmissionError
from novelty_agent_framework.agents import SearchPlannerAgent, SearchPlannerExhaustedError
from novelty_agent_framework.agents.search_plan_compiler import SearchPlanCompilationError, SemanticLimits
from novelty_agent_framework.schemas.failures import FailureCode as C
from test_search_planner import make_point, make_task, valid_draft, PROMPTS_ROOT, repair_reply
from test_workflow import build_workflow, make_paper, RecordingTaskResearcher


class Client:
    def __init__(self, *outputs):
        self.outputs = iter(outputs)
        self.calls = []

    def complete(self, messages, *, options=None):
        self.calls.append((messages, options))
        result = next(self.outputs)
        if isinstance(result, Exception):
            raise result
        if callable(result):
            result = result(options)
        return result if isinstance(result, ModelResponse) else ModelResponse(content=result)

    async def acomplete(self, messages, *, options=None):
        return self.complete(messages, options=options)


def good():
    return json.dumps(valid_draft(make_task()))


def http_error(status, retry_after=None):
    headers = Message()
    if retry_after is not None:
        headers['Retry-After'] = retry_after
    error = ModelClientError('HTTP error', failure_reason='http_status')
    error.__cause__ = urllib.error.HTTPError('https://model.test', status, 'error', headers, None)
    return error


def run(client, asynchronous, **kwargs):
    agent = SearchPlannerAgent(model_client=client, **kwargs)
    return asyncio.run(agent.aplan(make_point(), make_task())) if asynchronous else agent.plan(make_point(), make_task())


@pytest.mark.parametrize('asynchronous', [False, True])
@pytest.mark.parametrize('error,code', [
    (http_error(401), C.MODEL_HTTP_AUTH), (http_error(403), C.MODEL_HTTP_DENIED),
    (http_error(400), C.MODEL_HTTP_REJECTED),
    (ModelClientError('no key', failure_reason='model.config.credential_missing'), C.MODEL_CREDENTIAL_MISSING),
    (ModelClientError('unknown alias', failure_reason='model.config.alias_unknown'), C.MODEL_ALIAS_UNKNOWN),
    (ModelClientError('unsupported', failure_reason='model.config.provider_unsupported'), C.MODEL_PROVIDER_UNSUPPORTED),
    (ModelContextAdmissionError({'code': 'CONTEXT_LIMIT_EXCEEDED'}), C.MODEL_CONTEXT_LIMIT),
    (ModelContextAdmissionError({'code': 'MEASUREMENT_UNAVAILABLE'}), C.MODEL_CONTEXT_UNAVAILABLE),
    (ModelCallBudgetExceeded('budget'), C.MODEL_CALL_BUDGET),
    (ModelClientError('invalid envelope', failure_reason='model.response.envelope_invalid'), C.MODEL_RESPONSE_SHAPE),
    (RuntimeError('programming failure'), C.UNKNOWN),
])
def test_terminal_errors_never_reach_a_second_request(asynchronous, error, code):
    client = Client(error, good())
    with pytest.raises(SearchPlannerExhaustedError) as caught:
        run(client, asynchronous)
    assert caught.value.code == code
    assert caught.value.attempts == 1
    assert caught.value.terminal_reason == 'non_retryable'
    assert caught.value.__cause__ is error
    assert len(client.calls) == 1


@pytest.mark.parametrize('asynchronous', [False, True])
@pytest.mark.parametrize('error', [TimeoutError('timed out'), http_error(429, '0'), http_error(503)])
def test_transient_retry_preserves_request(asynchronous, error, monkeypatch):
    delays = []
    async def sleep(delay):
        delays.append(delay)
    monkeypatch.setattr('novelty_agent_framework.agents.search_planner.asyncio.sleep', sleep)
    client = Client(error, good())
    assert run(client, asynchronous).task_id == 'T-1'
    assert client.calls[0] == client.calls[1]
    assert delays == [0.5]


def test_retry_after_is_honored_and_excessive_wait_stops(monkeypatch):
    delays = []
    async def sleep(delay):
        delays.append(delay)
    monkeypatch.setattr('novelty_agent_framework.agents.search_planner.asyncio.sleep', sleep)
    run(Client(http_error(429, '3'), good()), True)
    assert delays == [3]
    client = Client(http_error(429, '60'), good())
    with pytest.raises(SearchPlannerExhaustedError):
        run(client, True)
    assert len(client.calls) == 1 and delays == [3]


@pytest.mark.parametrize('asynchronous', [False, True])
@pytest.mark.parametrize('prompts', [None, PromptLibrary(PROMPTS_ROOT)])
def test_schema_repair_has_failed_draft_field_path_and_configured_limits(asynchronous, prompts):
    broken = valid_draft(make_task())
    broken['concepts'][0]['importance'] = 9
    client = Client(json.dumps(broken), repair_reply(valid_draft(make_task())))
    plan = run(client, asynchronous, prompts=prompts, semantic_limits=SemanticLimits(max_concepts=3))
    repaired_prompt = client.calls[1][0][1].content
    assert '"importance": 9' in repaired_prompt
    assert '"path": ["concepts", 0, "importance"]' in repaired_prompt
    assert C.MODEL_SCHEMA_INVALID.value in repaired_prompt
    assert '"max_concepts": 3' in repaired_prompt
    assert (plan.task_id, plan.novelty_point_id) == ('T-1', 'NP-1')


def test_compilation_feedback_preserves_every_issue_and_failed_draft():
    broken = valid_draft(make_task())
    broken['concepts'][0]['alias'] = ['DGNN', 'dynamic GNN']
    broken['concepts'][1]['terms'] = ['efficient']
    client = Client(json.dumps(broken), repair_reply(valid_draft(make_task())))
    run(client, False, semantic_limits=SemanticLimits(max_alias_per_concept=0))
    content = client.calls[1][0][1].content
    assert C.PLANNER_TOO_MANY_ALIAS.value in content
    assert C.PLANNER_GENERIC_TERM.value in content
    assert '"alias": ["DGNN", "dynamic GNN"]' in content
    assert '最多保留 0 个' in content


def test_transport_retry_does_not_erase_repair_context(monkeypatch):
    async def sleep(delay):
        pass
    monkeypatch.setattr('novelty_agent_framework.agents.search_planner.asyncio.sleep', sleep)
    client = Client('broken draft', http_error(503), repair_reply(valid_draft(make_task())))
    run(client, True)
    assert client.calls[1] == client.calls[2]
    assert 'broken draft' in client.calls[2][0][1].content


@pytest.mark.parametrize('outputs,reason,count', [
    (['broken', 'broken', good()], 'repair_budget_exhausted', 2),
    ([http_error(503), http_error(503), good()], 'transport_budget_exhausted', 2),
])
def test_action_budget_does_not_reset_after_repeated_failure(outputs, reason, count, monkeypatch):
    async def sleep(delay):
        pass
    monkeypatch.setattr('novelty_agent_framework.agents.search_planner.asyncio.sleep', sleep)
    client = Client(*outputs)
    with pytest.raises(SearchPlannerExhaustedError) as caught:
        run(client, True, max_attempts=10)
    assert caught.value.terminal_reason == reason
    assert caught.value.attempts == len(client.calls) == count
    assert caught.value.history[-1]['action'] == 'stop'


def test_global_attempt_cap_bounds_combined_repair_and_transport(monkeypatch):
    client = Client('broken', http_error(503), good())
    with pytest.raises(SearchPlannerExhaustedError) as caught:
        run(client, True, max_attempts=2)
    assert caught.value.terminal_reason == 'attempt_limit'
    assert caught.value.code == C.MODEL_HTTP_SERVER
    assert len(client.calls) == 2


def test_one_attempt_configuration_disables_both_recovery_actions():
    for output in ['broken', http_error(503)]:
        client = Client(output, good())
        with pytest.raises(SearchPlannerExhaustedError) as caught:
            run(client, False, max_attempts=1)
        assert caught.value.attempts == len(client.calls) == 1
        assert caught.value.terminal_reason == 'attempt_limit'


def test_compiler_internal_failure_is_never_sent_to_model_for_repair(monkeypatch):
    def broken(*args, **kwargs):
        raise SearchPlanCompilationError('anchor invariant failed')
    monkeypatch.setattr('novelty_agent_framework.agents.search_planner.build_runtime_plan', broken)
    client = Client(good(), good())
    with pytest.raises(SearchPlannerExhaustedError) as caught:
        run(client, False)
    assert caught.value.code == C.PLANNER_COMPILE_INVALID
    assert len(client.calls) == 1


def test_explicit_truncation_stops_even_if_json_happens_to_be_valid():
    client = Client(ModelResponse(content=good(), raw={'choices': [{'finish_reason': 'length'}]}), good())
    with pytest.raises(SearchPlannerExhaustedError) as caught:
        run(client, False)
    assert caught.value.code == C.MODEL_OUTPUT_TRUNCATED and len(client.calls) == 1


def test_tool_calls_are_rejected_without_execution():
    client = Client(ModelResponse(content=good(), tool_calls=(
        ModelToolCall(id='forbidden', name='search', arguments={'query': 'x'}),)), repair_reply(valid_draft(make_task())))
    run(client, False)
    assert C.PLANNER_TOOL_CALL_FORBIDDEN.value in client.calls[1][0][1].content
    assert '"name": "search"' in client.calls[1][0][1].content


def test_http_response_json_error_is_not_a_draft_error():
    client = Client(json.JSONDecodeError('bad envelope', '<html>', 0), good())
    with pytest.raises(SearchPlannerExhaustedError) as caught:
        run(client, False)
    assert caught.value.code == C.MODEL_RESPONSE_SHAPE and len(client.calls) == 1


def test_repair_context_is_isolated_between_concurrent_tasks():
    async def scenario():
        class ConcurrentClient:
            def __init__(self):
                self.calls = {}
                self.entered = asyncio.Event()
            async def acomplete(self, messages, *, options=None):
                content = messages[1].content
                key = 'NP-2' if '"point_id": "NP-2"' in content else 'NP-1'
                calls = self.calls.setdefault(key, [])
                calls.append(content)
                if len(self.calls) == 2:
                    self.entered.set()
                await self.entered.wait()
                if len(calls) == 1:
                    return ModelResponse(content=f'bad-draft-{key}')
                other = 'NP-1' if key == 'NP-2' else 'NP-2'
                assert f'bad-draft-{key}' in content and f'bad-draft-{other}' not in content
                return ModelResponse(content=repair_reply(valid_draft(make_task()))(options))
        client = ConcurrentClient()
        planner = SearchPlannerAgent(model_client=client)
        point = make_point()
        task = make_task()
        plans = await asyncio.wait_for(asyncio.gather(planner.aplan(point, task), planner.aplan(
            point.model_copy(update={'point_id': 'NP-2'}),
            task.model_copy(update={'novelty_point_id': 'NP-2'}))), 3)
        assert {p.novelty_point_id for p in plans} == {'NP-1', 'NP-2'}
        assert all(len(calls) == 2 for calls in client.calls.values())
    asyncio.run(scenario())


def test_cancel_during_retry_backoff_does_not_start_another_call(monkeypatch):
    async def scenario():
        sleeping = asyncio.Event()
        async def sleep(delay):
            sleeping.set()
            await asyncio.Event().wait()
        monkeypatch.setattr('novelty_agent_framework.agents.search_planner.asyncio.sleep', sleep)
        client = Client(http_error(503), good())
        pending = asyncio.create_task(SearchPlannerAgent(model_client=client).aplan(make_point(), make_task()))
        await asyncio.wait_for(sleeping.wait(), 3)
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        assert len(client.calls) == 1
    asyncio.run(scenario())


def test_terminal_planner_error_cancels_siblings_and_never_dispatches_research(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    async def scenario():
        entered, cancelled = asyncio.Event(), asyncio.Event()
        class Planner:
            async def aplan(self, point, task):
                if task.task_id == 'T-1':
                    await entered.wait()
                    return await SearchPlannerAgent(model_client=Client('broken', 'broken')).aplan(point, task)
                entered.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    cancelled.set()
        workflow, researcher = build_workflow(planner=Planner(), max_concurrency=2, max_rounds=2)
        with pytest.raises(SearchPlannerExhaustedError) as caught:
            await asyncio.wait_for(workflow.arun(make_paper()), 5)
        assert caught.value.code == C.MODEL_JSON_INVALID
        assert cancelled.is_set() and researcher.calls == []
        root = next((tmp_path / 'outputs/paper-test/runtime').iterdir())
        assert json.loads((root / 'manifest.json').read_text())['status'] == 'FAILED'
        errors = [json.loads(p.read_text()) for p in (root / 'errors').glob('*.json')]
        terminal = next(e for e in errors if e.get('code') == C.MODEL_JSON_INVALID.value)
        assert terminal['audit']['terminal_reason'] == 'repair_budget_exhausted'
        assert terminal['audit']['attempts'] == 2
        assert not list((tmp_path / 'outputs/paper-test/research-runs').glob('**/*.json'))
    asyncio.run(scenario())


def test_supplement_planner_failure_preserves_prior_results_and_stops(tmp_path, monkeypatch):
    from novelty_agent_framework.agents import DemoSearchPlanner
    monkeypatch.chdir(tmp_path)

    class Planner(DemoSearchPlanner):
        async def aplan(self, point, task):
            if task.attempt == 1:
                return self.plan(point, task)
            return await SearchPlannerAgent(model_client=Client('broken', 'broken')).aplan(point, task)

    researcher = RecordingTaskResearcher(first_round_empty=True)
    workflow, _ = build_workflow(researcher=researcher, planner=Planner(), max_rounds=3)
    with pytest.raises(SearchPlannerExhaustedError) as caught:
        workflow.run(make_paper())
    assert caught.value.code == C.MODEL_JSON_INVALID
    assert researcher.calls and all(c.research_task.attempt == 1 for c in researcher.calls)
    records = list((tmp_path / 'outputs/paper-test/research-runs').glob('**/attempt-1.json'))
    assert len(records) == len(researcher.calls)
    assert not list((tmp_path / 'outputs/paper-test/research-runs').glob('**/attempt-2.json'))
