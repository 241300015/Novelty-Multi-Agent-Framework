"""Required discovery happens before any model response, with ordinary I/O budgets."""
import asyncio
import json

import pytest

from backend.env import ModelResponse, ModelToolCall
from novelty_agent_framework.core.tool_call_harness import ToolCallHarness, ToolCallHarnessConfig
from novelty_agent_framework.schemas import ResearcherToolObservation
from novelty_agent_framework.tools import ResearcherToolRegistry
from novelty_agent_framework.workflows.research_reading_policy import ResearchReadingPolicy
from test_research_reading_policy import Database, task_scope


class EmptyDatabase(Database):
    def __init__(self, *, succeeds=True):
        self.calls = 0
        self.succeeds = succeeds

    async def ainvoke(self, arguments, *, scope):
        self.calls += 1
        assert arguments.source_id is None
        return ResearcherToolObservation(tool_name=self.name, succeeded=self.succeeds,
            summary='initial search attempted', error=None if self.succeeds else 'provider unavailable',
            payload={'research_bundle': {'source_records': [], 'artifacts': []}})


@pytest.mark.parametrize('text_choices', [True, False])
@pytest.mark.parametrize('succeeds', [True, False])
def test_initial_search_is_not_a_model_choice_or_model_turn(text_choices, succeeds):
    db = EmptyDatabase(succeeds=succeeds)
    class Model:
        calls = 0
        async def acomplete(self, messages, *, options):
            self.calls += 1
            assert db.calls == 3
            assert any('自动执行首轮检索' in (m.content or '') for m in messages)
            assert any('第 2 轮检索' in (m.content or '') for m in messages)
            assert any('第 3 轮检索' in (m.content or '') for m in messages)
            assert any('initial search attempted' in (m.content or '') for m in messages)
            assert not any(m.role in {'assistant', 'tool'} or m.tool_calls for m in messages)
            assert 'Current Harness choices' not in messages[-1].content
            assert options.tools == () and options.tool_choice == 'none'
            return ModelResponse(content='{"cards":[],"no_evidence_reason":"No usable result"}')
    model = Model()
    harness = ToolCallHarness(model, ResearcherToolRegistry([db]),
        config=ToolCallHarnessConfig(text_choices=text_choices, max_turns=2),
        action_policy_factory=ResearchReadingPolicy)
    result = asyncio.run(harness.run(system_prompt='test', initial_user_message='task', scope=task_scope()))
    assert result.turns_used == model.calls == 1
    assert result.tool_calls_used == db.calls == 3
    kinds = [e.kind for e in result.trace]
    assert kinds.index('tool_call') < kinds.index('assistant_response')


def test_automatic_search_consumes_tool_budget_and_preserves_result_for_finalization():
    db = EmptyDatabase()
    class Model:
        async def acomplete(self, messages, *, options):
            assert db.calls == 1
            assert options.tools == () and options.tool_choice == 'none'
            assert any('initial search attempted' in (m.content or '') for m in messages)
            return ModelResponse(content='{"cards":[],"no_evidence_reason":"Tool budget exhausted"}')
    harness = ToolCallHarness(Model(), ResearcherToolRegistry([db]),
        config=ToolCallHarnessConfig(text_choices=True, max_turns=2, max_tool_calls=1,
            reserve_final_turn=True, finalize_on_budget=True), action_policy_factory=ResearchReadingPolicy)
    result = asyncio.run(harness.run(system_prompt='test', initial_user_message='task', scope=task_scope()))
    assert result.tool_calls_used == 1 and result.turns_used == 1
    assert result.stop_reason is None
    assert result.execution_limits[0]['code'].value == 'harness.budget.tool_calls_exhausted'
