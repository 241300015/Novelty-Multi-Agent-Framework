"""Adapt shared whole-plan test recipes to the two live response contracts.

Compiler fixtures remain whole plans. This model stub emits their concepts and
strategies on separate requests; strict wire-contract tests use raw clients in
test_planner_two_stage.py instead.
"""
import json
from backend.env import ModelResponse


def stage_of(messages):
    return 'strategies' if 'Harness 当前阶段：strategies' in messages[1].content else 'concepts'


def stage_payload(value, stage):
    if isinstance(value, dict) and 'concepts' in value and 'strategies' in value:
        return {key: item for key, item in value.items() if key == stage or key not in {'concepts','strategies'}}
    return value


class PlannerScript:
    def __init__(self, outputs, *, repeat_last=False):
        self.outputs = list(outputs)
        self.index = 0
        self.last_plan = None
        self.strategy_started = False
        self.repeat_last = repeat_last

    def respond(self, messages, options):
        stage = stage_of(messages)
        if stage == 'strategies' and not self.strategy_started and self.last_plan is not None:
            self.strategy_started = True
            return ModelResponse(content=json.dumps({'strategies':self.last_plan['strategies']}))
        if self.index >= len(self.outputs):
            if not self.repeat_last:
                raise AssertionError('script exhausted')
            output = self.outputs[-1]
        else:
            output = self.outputs[self.index]
            self.index += 1
        if isinstance(output, Exception):
            raise output
        recipe = getattr(output, 'planner_draft', None)
        if isinstance(recipe, dict) and {'concepts','strategies'} <= recipe.keys():
            self.last_plan = recipe
        value = (output.with_messages(options, messages) if hasattr(output, 'with_messages') else
                 output(options) if callable(output) else output)
        response = value if isinstance(value, ModelResponse) else ModelResponse(content=value)
        try:
            payload = json.loads(response.content)
        except (ValueError, TypeError):
            return response
        if isinstance(payload, dict) and {'concepts','strategies'} <= payload.keys():
            self.last_plan = payload
            response = ModelResponse(content=json.dumps(stage_payload(payload, stage)),
                                     raw=response.raw, tool_calls=response.tool_calls)
        return response
