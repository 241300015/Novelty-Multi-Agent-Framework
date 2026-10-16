"""Exercise the formal loop, including normal and forced draft transitions."""
import asyncio
import json

import pytest
from backend.env import ModelCallOptions, ModelResponse, ModelToolCall
from novelty_agent_framework.core.tool_call_harness import ToolCallHarness, ToolCallHarnessConfig
from novelty_agent_framework.schemas.failures import FailureCode as C
from novelty_agent_framework.tools import ResearcherToolRegistry


class Menu:
    def __init__(self, *_):
        self.step = 0

    @property
    def can_finish(self):
        return self.step == 2

    def choices(self):
        return {} if self.can_finish else {f'v{self.step}_0': '继续', f'v{self.step}_1': '停止'}

    def selection(self, arguments, call_id):
        return ModelToolCall(id=call_id, name='choose', arguments=arguments)

    def resolve(self, call):
        assert call.arguments['option_id'] in self.choices()
        self.step += 1
        return None


@pytest.mark.parametrize('forced', [False, True])
def test_formal_harness_replaces_current_enum_and_restores_draft_contract(forced):
    draft_format = {'type': 'json_object'}
    original = ModelCallOptions(temperature=0.3, max_tokens=4096, response_format=draft_format)
    calls = []

    class Client:
        async def acomplete(self, messages, *, options):
            calls.append(options)
            assert options.tools == () and options.tool_choice == 'none'
            assert options.temperature == 0.3 and options.max_tokens == 4096
            if len(calls) <= (1 if forced else 2):
                menu = json.loads(messages[-1].content.split('\n', 1)[1].split('\nReturn', 1)[0])
                spec = options.response_format['json_schema']
                assert spec['strict'] and spec['schema']['required'] == ['option_id']
                assert not spec['schema']['additionalProperties']
                assert spec['schema']['properties']['option_id']['enum'] == list(menu)
                return ModelResponse(content=json.dumps({'option_id':next(iter(menu))}))
            assert options.response_format == draft_format
            return ModelResponse(content='{"cards":[],"no_evidence_reason":"test"}')

    harness = ToolCallHarness(Client(), ResearcherToolRegistry([]),
        config=ToolCallHarnessConfig(text_choices=True, max_turns=2 if forced else 3,
            finalize_on_budget=forced, reserve_final_turn=forced), action_policy_factory=Menu)
    result = asyncio.run(harness.run(system_prompt='system', initial_user_message='task', scope=None, options=original))
    assert result.tool_calls_used == 0
    assert 'cards' in result.final_content
    assert original.response_format == draft_format
    if forced:
        assert any(e.failure_code == C.BUDGET_TURNS for e in result.trace)
    else:
        assert calls[0].response_format != calls[1].response_format
