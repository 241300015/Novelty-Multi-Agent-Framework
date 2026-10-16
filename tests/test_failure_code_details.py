"""Specific observed causes remain distinguishable without changing recovery policy."""
import asyncio

import pytest
from pydantic import BaseModel, ValidationError
from backend.env import ModelResponse
from novelty_agent_framework.schemas.failures import (
    FailureCode as C, FAILURE_CATALOG, FAILURE_FAMILIES, FailureEvent, CodedFailure,
    failure_family, make_failure,
)
from novelty_agent_framework.core.failure_classification import output_failure_code
from novelty_agent_framework.workflows.research_reading_policy import ResearchReadingPolicy
from novelty_agent_framework.tools import ResearcherToolRegistry
from test_evidence_card_builder import builder, scope, read, quote, card, finish, TEXT_A
from test_research_reading_policy import Database, Reader, task_scope, choose


def test_all_codes_have_policy_and_leafs_preserve_bounded_family_advice():
    assert set(FAILURE_CATALOG) == set(C)
    for code, family in FAILURE_FAMILIES.items():
        leaf, parent = FAILURE_CATALOG[code], FAILURE_CATALOG[family]
        assert (leaf.actions, leaf.retry_limit, leaf.input_change, leaf.effect) == (
            parent.actions, parent.retry_limit, parent.input_change, parent.effect)
        event = make_failure(code, scope={'node':'test.boundary', 'point_id':'NP-1'})
        assert FailureEvent.model_validate_json(event.model_dump_json()) == event
        assert failure_family(event.code) == family


def test_legacy_failure_without_node_remains_readable():
    event = make_failure(C.MODEL_OUTPUT_REFERENCE, scope={'point_id':'NP-1'})
    data = event.model_dump(mode='json')
    data['scope'].pop('node')
    assert FailureEvent.model_validate(data) == event


@pytest.mark.parametrize('value,expected', [
    ('Input novelty claim about GSAERU is not a reference quotation.', C.BUILDER_QUOTE_TARGET),
    (TEXT_A.replace('Alpha', 'Alphi'), C.BUILDER_QUOTE_TEXT_MISMATCH),
    ('A completely unrelated statement from nowhere.', C.BUILDER_QUOTE_UNGROUNDED),
])
def test_builder_distinguishes_target_input_small_text_mismatch_and_unknown(tmp_path, value, expected):
    request = scope()
    request.novelty_point.claim_en = 'Input novelty claim about GSAERU is not a reference quotation.'
    result = builder(tmp_path).build(finish(card(quote(value))), scope=request, read_results=[read()])
    assert not result.evidence_cards and not result.evidence
    assert result.rejections[0].code == expected
    assert result.rejections[0].node == 'builder.compile'


def test_quote_shared_with_target_is_accepted_if_real_reference_supports_it(tmp_path):
    request = scope()
    request.novelty_point.claim = 'Alpha unique quote.'
    result = builder(tmp_path).build(finish(card(quote(request.novelty_point.claim))), scope=request, read_results=[read()])
    assert len(result.evidence_cards) == 1 and not result.rejections


def test_known_previous_option_differs_from_label_and_unissued_id():
    from backend.env import ModelToolCall
    policy = ResearchReadingPolicy(ResearcherToolRegistry([Database(), Reader()]), task_scope())
    first = choose(policy, 'arxiv')
    label = next(iter(policy.choices().values()))
    for value, code in [(label, C.HARNESS_OPTION_LABEL), ('v999_0', C.HARNESS_OPTION_UNKNOWN)]:
        with pytest.raises(CodedFailure) as error:
            policy.resolve(ModelToolCall(id='bad', name=first.name, arguments={'option_id':value}))
        assert error.value.code == code
    call = policy.resolve(first)
    result = asyncio.run(policy.registry.execute(call.name, call.arguments, scope=policy.execution_scope(call)))
    policy.observe(call, result)
    with pytest.raises(CodedFailure) as error:
        policy.resolve(first)
    assert error.value.code == C.HARNESS_OPTION_STALE


def test_invalid_json_and_wrong_json_shape_do_not_share_a_code():
    class Payload(BaseModel):
        count: int
    for value, expected in [('not json', C.MODEL_JSON_INVALID), ('{"count": []}', C.MODEL_SCHEMA_INVALID)]:
        with pytest.raises(ValidationError) as error:
            Payload.model_validate_json(value)
        assert output_failure_code(error.value) == expected


def test_correction_keeps_before_and_after_error_codes_in_persistable_result(tmp_path):
    from test_card_recovery import workflow, Reader as CardReader, ScriptedModelClient, call
    request = scope()
    request.novelty_point.claim = 'GSAERU is a claim from the target paper.'
    wrong = finish(card(quote(request.novelty_point.claim)))
    model = ScriptedModelClient(call('a'),
        ModelResponse(content=wrong.model_dump_json()),
        ModelResponse(content=wrong.model_dump_json()))
    result = asyncio.run(workflow(tmp_path, model, CardReader([read()])).ainvoke(request))
    encoded = result.model_dump(mode='json')
    assert encoded['card_build_rejections'][0]['code'] == C.BUILDER_QUOTE_TARGET
    assert encoded['card_correction_rejections'][0]['code'] == C.BUILDER_QUOTE_TARGET
    assert encoded['card_correction_rejections'][0]['node'] == 'builder.correction'
    assert not result.evidence_cards


@pytest.mark.parametrize('value,expected', [
    ({}, C.MODEL_TOOL_LIST), ([1], C.MODEL_TOOL_ITEM),
    ([{'function':{}}], C.MODEL_TOOL_ID),
    ([{'id':'t'}], C.MODEL_TOOL_FUNCTION),
    ([{'id':'t','function':{}}], C.MODEL_TOOL_NAME),
    ([{'id':'t','function':{'name':'reader','arguments':{}}}], C.MODEL_TOOL_ARGS_TYPE),
    ([{'id':'t','function':{'name':'reader','arguments':'broken'}}], C.MODEL_TOOL_ARGS_JSON),
    ([{'id':'t','function':{'name':'reader','arguments':'[]'}}], C.MODEL_TOOL_ARGS_OBJECT),
])
def test_model_response_shape_errors_are_not_inferred_from_message_text(value, expected):
    from backend.env.model_client import _parse_tool_calls, ModelClientError
    from novelty_agent_framework.core.failure_classification import model_failure_code
    with pytest.raises(ModelClientError) as error:
        _parse_tool_calls(value)
    error.value.args = ('redacted message',)
    assert model_failure_code(error.value) == expected


def test_scope_denial_is_distinct_from_file_permission_denial():
    from novelty_agent_framework.tools.researcher_registry import _failure_payload
    import errno
    denied_scope = _failure_payload(PermissionError('outside scope'), scope(), 'reader', registered=True, phase='invoke')
    denied_file = _failure_payload(PermissionError(errno.EACCES, 'denied', '/tmp/file'), scope(), 'reader', registered=True, phase='invoke')
    assert denied_scope['failure']['code'] == C.TOOL_SCOPE_DENIED
    assert denied_file['failure']['code'] == C.READER_ACCESS_DENIED
