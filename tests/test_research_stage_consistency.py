"""Regression coverage for stage contracts, capability filtering and temporary correction."""
import asyncio
import json
from types import SimpleNamespace

import pytest

from backend.env import ModelResponse, ModelToolCall
from novelty_agent_framework.core.tool_call_harness import ToolCallHarness, ToolCallHarnessConfig
from novelty_agent_framework.schemas import ResearchFinishDraft, ResearcherToolObservation
from novelty_agent_framework.schemas.failures import FailureCode as C
from novelty_agent_framework.tools import ResearcherToolRegistry
from novelty_agent_framework.workflows.research_reading_policy import ResearchReadingPolicy
from test_research_reading_policy import Database, Reader, MenuModel, task_scope
from test_initial_research_search import EmptyDatabase


FINAL = '{"cards":[],"no_evidence_reason":"仅保留实际观察，无可用引句。"}'
SCHEMA = ResearchFinishDraft.model_json_schema()


def run(model, tools, *, scope=None, policy=ResearchReadingPolicy, **config):
    harness = ToolCallHarness(model, ResearcherToolRegistry(tools),
        config=ToolCallHarnessConfig(final_schema=SCHEMA, final_system_prompt='FINAL CONTRACT',
            **config), action_policy_factory=policy)
    return asyncio.run(harness.run(system_prompt='ACTION CONTRACT', initial_user_message='task',
        scope=scope or task_scope()))


def assert_final(messages, options):
    assert messages[0].content == 'FINAL CONTRACT'
    assert 'Current Harness choices' not in messages[-1].content
    assert options.tools == () and options.tool_choice == 'none'
    assert options.response_format['json_schema']['schema'] == SCHEMA


@pytest.mark.parametrize('text_choices', [True, False])
@pytest.mark.parametrize('bad', ['unknown', 'mixed', 'early', 'raw'])
def test_repeated_error_gets_one_correction_then_single_finalization(text_choices, bad):
    class Model:
        calls = 0
        async def acomplete(self, messages, *, options):
            self.calls += 1
            # Native protocol always completes a tool call before adding feedback.
            pending = set()
            for message in messages:
                if message.tool_calls:
                    assert not pending
                    pending.update(c.id for c in message.tool_calls)
                elif message.role == 'tool':
                    assert message.tool_call_id in pending
                    pending.remove(message.tool_call_id)
                else:
                    assert not pending
            assert not pending
            if self.calls == 3:
                assert_final(messages, options)
                assert any('temporary repeated decision error' in (m.content or '') for m in messages)
                assert any('test database' in (m.content or '') or 'Paper 1' in (m.content or '') for m in messages)
                return ModelResponse(content=FINAL)
            if self.calls == 2:
                assert any('临时策略' in (m.content or '') for m in messages)
            if bad == 'early':
                return ModelResponse(content=FINAL)
            if bad == 'raw':
                return ModelResponse(content=None, tool_calls=(ModelToolCall(
                    id=f'raw{self.calls}', name='reader', arguments={'artifact_id':'forged'}),))
            args = {'option_id': 'ResearchFinishDraft' if bad == 'mixed' else 'unknown'}
            if bad == 'mixed':
                args.update(json.loads(FINAL))
            if text_choices:
                return ModelResponse(content=json.dumps(args))
            return ModelResponse(content=None, tool_calls=(ModelToolCall(
                id=f'choice{self.calls}', name='choose_research_action', arguments=args),))
    model, reader = Model(), Reader()
    result = run(model, [Database(), reader], text_choices=text_choices,
        max_turns=100, repeat_error_limit=2, finalize_on_budget=True)
    assert model.calls == result.turns_used == 3
    assert result.tool_calls_used == 1 and not reader.calls
    assert result.stop_code in {C.HARNESS_OPTION_UNKNOWN, C.HARNESS_DECISION_SCHEMA,
                                C.HARNESS_EARLY_FINISH, C.HARNESS_NATIVE_CALL}
    assert result.trace[-1].detail == 'temporary decision-error finalization'
    assert sum(e.detail == 'temporary_decision_correction' for e in result.trace) == 1


@pytest.mark.parametrize('budget', [None, 1])
def test_normal_and_budget_finish_use_same_schema_without_decision_routing(budget):
    class Model:
        calls = 0
        async def acomplete(self, messages, *, options):
            self.calls += 1
            assert_final(messages, options)
            # Malformed finish belongs to the outer result parser, never action dispatch.
            return ModelResponse(content='{"option_id":"ResearchFinishDraft","cards":[]}')
    model, db = Model(), EmptyDatabase()
    config = {} if budget is None else {'max_tool_calls': budget}
    result = run(model, [db], text_choices=True, repeat_error_limit=2, **config)
    assert model.calls == 1 and db.calls == (3 if budget is None else 1)
    assert not any(e.failure_code in {C.HARNESS_OPTION_UNKNOWN, C.HARNESS_DECISION_SCHEMA}
                   for e in result.trace)


@pytest.mark.parametrize('missing', ['database_search', 'reader'])
def test_missing_required_capability_is_recorded_not_silently_completed(missing):
    model = MenuModel()
    tools = [Reader()] if missing == 'database_search' else [Database()]
    result = run(model, tools, text_choices=True)
    assert any(item['tool'] == missing and item['code'] == C.TOOL_NOT_REGISTERED
               and item['required'] for item in result.execution_limits)
    assert result.final_content


def test_harness_clips_read_request_and_preserves_unread_material_limit():
    class BoundedReader(Reader):
        async def ainvoke(self, arguments, *, scope):
            self.calls.append(arguments)
            return ResearcherToolObservation(tool_name=self.name, succeeded=True, payload={'read_result': {
                'artifact_id': arguments.artifact_id, 'text': 'x' * arguments.max_chars,
                'char_start': 0, 'char_end': arguments.max_chars, 'has_more': True}})
    reader = BoundedReader()
    result = run(MenuModel(), [Database(), reader], text_choices=True, max_total_read_chars=1000)
    assert len(reader.calls) == 1 and reader.calls[0].max_chars == 1000
    assert any(i['code'] == C.BUDGET_READ_CHARS and i['required'] for i in result.execution_limits)


def test_metadata_and_duplicate_returns_allow_stop_only_after_handling_candidates():
    policy = ResearchReadingPolicy(ResearcherToolRegistry([Database(), Reader()]), task_scope())
    observation = ResearcherToolObservation(tool_name='database_search', succeeded=True,
        payload={'research_bundle': {'source_records': [{'source_record_id':'src_1',
            'source_id':'arxiv', 'title':'Metadata-only paper'}], 'artifacts':[]}})
    policy.observe(policy.automatic_tool_call(), observation)
    assert any('暂无摘要材料' in label for label in policy.choices().values())
    assert not any(label.startswith('停止检索') for label in policy.choices().values())
    def choose(label):
        key = next(k for k,v in policy.choices().items() if label in v)
        return policy.resolve(policy.selection({'option_id':key}, 'selected'))
    choose('不获取')
    assert any('停止检索' in label for label in policy.choices().values())
    call = choose('第 2 轮')
    policy.observe(call, observation)
    assert policy.last_round_returned
    assert any('停止检索' in label for label in policy.choices().values())


@pytest.mark.parametrize('text_choices', [True, False])
def test_cached_automatic_read_advances_policy_without_new_physical_read(text_choices):
    class CachedReader(Reader):
        async def ainvoke(self, arguments, *, scope):
            observation = await super().ainvoke(arguments, scope=scope)
            observation.payload['read_result'].update(
                read_id='rd_test', namespace='research', sha256='a' * 64)
            return observation
    class ReplayPolicy(ResearchReadingPolicy):
        observations_seen = 0
        def observe(self, call, observation):
            self.observations_seen += 1
            if self.observations_seen == 1:
                # Exercise a recovery policy revisiting a previously returned segment.
                self._automatic(self._read('bound_body', 0, 'cached segment'), '复用已有读取')
            else:
                super().observe(call, observation)
    scope = SimpleNamespace(recovery=SimpleNamespace(action=SimpleNamespace(value='read_artifact'),
        artifacts=[SimpleNamespace(artifact_id='bound_body')]))
    class Model:
        async def acomplete(self, messages, *, options):
            assert_final(messages, options)
            assert not any(m.role == 'tool' or m.tool_calls for m in messages)
            return ModelResponse(content=FINAL)
    reader = CachedReader()
    result = run(Model(), [reader], scope=scope, policy=ReplayPolicy,
        text_choices=text_choices, reuse_reader_results=True, max_tool_calls=1)
    assert len(reader.calls) == result.tool_calls_used == 1
    assert sum(e.detail == 'reader_state_reused' for e in result.trace) == 1
    assert sum(e.observation is not None for e in result.trace) == 1
    assert not result.execution_limits


def test_one_correctable_error_in_each_stage_does_not_accumulate_across_stages():
    class Model(MenuModel):
        seen = None
        async def acomplete(self, messages, *, options):
            current = messages[-1].content
            if 'Current Harness choices' in current:
                if self.seen is None:
                    self.seen = set()
                if current not in self.seen:
                    self.seen.add(current)
                    return ModelResponse(content='{"option_id":"unknown"}')
            return await super().acomplete(messages, options=options)
    reader = Reader()
    result = run(Model(), [Database(), reader], text_choices=True,
                 max_turns=30, repeat_error_limit=2, finalize_on_budget=True)
    assert result.stop_reason is None
    assert len(reader.calls) == 2
    assert sum(e.detail == 'temporary_decision_correction' for e in result.trace) > 1


def test_workflow_repairs_mixed_final_as_result_and_marks_repeated_decision_partial():
    from novelty_agent_framework.workflows.research_task import TaskResearcherWorkflow, TaskResearcherConfig
    from test_task_researcher_workflow import FakeModel, FakeBuilder
    mixed = ModelResponse(content='{"option_id":"ResearchFinishDraft","cards":[],"no_evidence_reason":"empty"}')
    for database, responses, expected_status, steps in [
        (EmptyDatabase(), [mixed, ModelResponse(content=FINAL)], 'completed', 2),
        (Database(), [mixed, mixed, ModelResponse(content=FINAL)], 'partial', 3),
    ]:
        model = FakeModel(responses)
        workflow = TaskResearcherWorkflow(model, ResearcherToolRegistry([database, Reader()]), FakeBuilder(),
            config=TaskResearcherConfig(guided_reading=True, text_choices=True))
        result = asyncio.run(workflow.ainvoke(task_scope()))
        assert result.status.value == expected_status
        assert len(model.messages) == result.steps_used == steps
        if expected_status == 'partial':
            assert any(f.code == C.HARNESS_DECISION_SCHEMA for f in result.execution_failures)
            assert any('temporary repeated decision error' in w for w in result.warnings)
        else:
            assert any('single format repair' in w for w in result.warnings)
