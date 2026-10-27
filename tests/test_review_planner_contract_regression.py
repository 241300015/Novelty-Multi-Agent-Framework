"""Regressions from the 2026-10-26 local run: enums, feature IDs, no-op repair."""
import asyncio
import json

import pytest

from backend.env import ModelResponse
from novelty_agent_framework.agents.evidence_reviewer import (
    _bound_review_schema, NoveltyEvidenceReviewer,
)
from novelty_agent_framework.schemas.domain import ReviewerSummaryDraft, ReviewerSupportDraft
from novelty_agent_framework.schemas.failures import FailureCode as C
from novelty_agent_framework.agents import SearchPlannerAgent, SearchPlannerExhaustedError
from test_novelty_point_reviewer import _request
from test_planner_recovery import Client as PlannerClient
from test_planner_scope_correction import updates_reply
from test_search_planner import valid_draft, make_task, make_point
from test_reviewer_controlled_harness import ChoosingClient, controlled, row
from test_reviewer_summary_checkpoint import Client, output


def review_request():
    request = _request()
    return request.model_copy(update={'novelty_point': request.novelty_point.model_copy(
        update={'technical_features': ['Uses a leader-worker training scheme.']})})


def run(client, asynchronous, **kwargs):
    agent = SearchPlannerAgent(model_client=client, **kwargs)
    point, task = make_point(), make_task(language='en')
    return asyncio.run(agent.aplan(point, task)) if asynchronous else agent.plan(point, task)


def test_controlled_final_draft_and_repair_keep_enum_and_feature_constraints():
    request = review_request()
    class InvalidThenFixed(ChoosingClient):
        async def acomplete(self, messages, *, options=None):
            if messages[0].content.startswith('Repair the format'):
                self.calls.append((messages, options))
                payload = json.loads(messages[1].content)
                assert 'basis_type' in payload['validation_error']
                draft = json.loads(payload['previous_output'])
                draft['feature_comparisons'][0]['basis_type'] = 'insufficient'
                return ModelResponse(content=json.dumps(draft))
            response = await super().acomplete(messages, options=options)
            if 'option_id' in json.loads(response.content):
                return response
            draft = json.loads(response.content)
            draft['feature_comparisons'] = [{
                'feature_id': 'F1', 'work_id': request.evidence[0].work_id,
                'relation': 'unknown', 'basis_type': 'unknown',
                'evidence_refs': [], 'reason': 'Not established by visible evidence.',
            }]
            return ModelResponse(content=json.dumps(draft))
    client = InvalidThenFixed(request, ['可用', '不继续'])
    result = asyncio.run(controlled(client, fail_closed=False).review_card(request))
    assert result.status.value == 'reviewed'
    assert result.feature_comparisons[0].basis_type == 'insufficient'
    original_options, repair_options = client.calls[-2][1], client.calls[-1][1]
    assert original_options.response_format == repair_options.response_format
    schema = repair_options.response_format['json_schema']['schema']
    props = schema['$defs']['FeatureComparison']['properties']
    assert 'unknown' not in props['basis_type']['enum']
    assert 'unknown' in props['relation']['enum']
    assert 'F1' in props['feature_id']['enum'] and 'f1' not in props['feature_id']['enum']


def test_summary_supplies_same_feature_catalog_and_contract_through_repair():
    request = review_request()
    card_client = ChoosingClient(request, ['可用', '不继续'])
    card = asyncio.run(controlled(card_client).review_card(request))
    broken = {'novelty_point_id': request.novelty_point.point_id, 'status': 'reviewed'}
    client = Client(ModelResponse(content=json.dumps(broken)), output(request, reviewed=True))
    reviewer = NoveltyEvidenceReviewer(client)
    review = asyncio.run(reviewer.summarize_reviews(request, [row(request, card)]))
    assert review.status.value == 'reviewed'
    user = json.loads(client.calls[0][0][1].content)
    assert user['feature_catalog'] == [
        {'feature_id': f'F{i}', 'text': text, 'sha256': item['sha256']}
        for i, (text, item) in enumerate(zip(request.novelty_point.technical_features, user['feature_catalog']), 1)
    ]
    assert len(user['feature_catalog']) == len(request.novelty_point.technical_features)
    fmt = client.calls[0][1].response_format
    assert fmt == client.calls[1][1].response_format
    assert fmt['json_schema']['schema'] == user['review_schema']
    schema = user['review_schema']
    assert schema['$defs']['FeatureComparison']['properties']['feature_id']['enum'] == [
        f'F{i}' for i in range(1, len(request.novelty_point.technical_features) + 1)]
    reviewed, insufficient = schema['anyOf']
    assert reviewed['properties']['status']['const'] == 'reviewed'
    assert set(reviewed['required']) == {'novelty_point_id', 'status', 'verdict', 'verdict_reason', 'confidence'}
    for branch in (reviewed, insufficient):
        assert branch['type'] == 'object' and branch['additionalProperties'] is False
        assert {'novelty_point_id', 'status'} <= set(branch['required'])
        assert branch['properties']['novelty_point_id']['enum'] == [request.novelty_point.point_id]
    assert reviewed['properties']['verdict_reason']['minLength'] == 1
    assert reviewed['properties']['confidence']['minimum'] == 0
    assert insufficient['properties']['verdict'] == {'type': 'null'}
    assert 'requires verdict' in json.loads(client.calls[1][0][1].content)['validation_error']


def test_no_features_forbids_comparisons_without_empty_enum():
    request = _request()
    request = request.model_copy(update={'novelty_point': request.novelty_point.model_copy(update={'technical_features': []})})
    for model in (ReviewerSupportDraft, ReviewerSummaryDraft):
        schema = _bound_review_schema(model, request)
        assert schema['properties']['feature_comparisons']['maxItems'] == 0
        assert 'enum' not in schema['$defs']['FeatureComparison']['properties']['feature_id']


def chinese_alias_draft():
    draft = valid_draft(make_task(language='en'))
    draft['concepts'][0]['alias'] = ['内存消耗']
    draft['concepts'][1]['alias'] = ['训练时间']
    return draft


@pytest.mark.parametrize('asynchronous', [False, True])
def test_first_repair_updates_only_chinese_aliases_and_recompiles(asynchronous):
    broken = chinese_alias_draft()
    client = PlannerClient(json.dumps(broken), updates_reply({
        'field_0': ['memory usage'], 'field_1': ['training duration']}))
    plan = run(client, asynchronous)
    assert len(client.calls) == 2
    content = client.calls[1][0][1].content
    feedback, _ = json.JSONDecoder().raw_decode(content[content.index('{"stage":'):])
    assert [v['path'] for v in feedback['fields'].values()] == [
        ['concepts', 0, 'alias'], ['concepts', 1, 'alias']]
    assert plan.concepts[0].alias == ['memory usage']
    assert plan.concepts[1].alias == ['training duration']
    assert [c.terms for c in plan.concepts] == [c['terms'] for c in broken['concepts']]


def test_unchanged_repair_gets_feedback_once_and_keeps_original_scope():
    client = PlannerClient(json.dumps(chinese_alias_draft()),
        updates_reply({'field_0': ['内存消耗'], 'field_1': ['训练时间']}),
        updates_reply({'field_0': ['memory usage'], 'field_1': ['training duration']}))
    assert run(client, True).concepts[0].alias == ['memory usage']
    assert len(client.calls) == 3
    first, corrected = [json.JSONDecoder().raw_decode(messages[1].content[messages[1].content.index('{"stage":'):])[0]
                        for messages, _ in client.calls[1:]]
    assert corrected['protocol_error'] == C.PLANNER_REPAIR_NO_PROGRESS.value
    assert corrected['previous_output'] == first['previous_output']
    assert corrected['fields'] == first['fields']
    assert corrected['allowed_paths'] == first['allowed_paths']
    assert set(first['options']).isdisjoint(corrected['options'])


def test_repeated_noop_cannot_expand_budget_and_is_not_success():
    client = PlannerClient(json.dumps(chinese_alias_draft()), updates_reply({}), updates_reply({}))
    with pytest.raises(SearchPlannerExhaustedError) as caught:
        run(client, True, max_attempts=10)
    assert len(client.calls) == 3
    assert caught.value.code == C.PLANNER_REPAIR_NO_PROGRESS
    assert caught.value.terminal_reason == 'protocol_budget_exhausted'
    assert caught.value.history[-1]['repair']['revalidation'] == 'not_run'


def test_decline_and_total_attempt_cap_still_stop_noop_repair():
    client = PlannerClient(json.dumps(chinese_alias_draft()), updates_reply({}))
    with pytest.raises(SearchPlannerExhaustedError) as caught:
        run(client, False, max_attempts=2)
    assert caught.value.terminal_reason == 'attempt_limit'
    assert len(client.calls) == 2


def test_assembled_aliases_still_require_english_compilation():
    client = PlannerClient(json.dumps(chinese_alias_draft()),
        updates_reply({'field_0': ['新的中文别名'], 'field_1': ['training time']}))
    with pytest.raises(SearchPlannerExhaustedError) as caught:
        run(client, False, max_attempts=10)
    assert caught.value.code == C.PLANNER_MISSING_EN_TERM
    assert caught.value.terminal_reason == 'repair_budget_exhausted'
    assert len(client.calls) == 2
