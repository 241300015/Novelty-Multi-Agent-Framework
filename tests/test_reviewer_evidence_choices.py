"""Evidence references are Harness-issued choices, refreshed after actual reads."""
import asyncio
import json
from dataclasses import replace

from backend.env import ModelResponse
from novelty_agent_framework.agents.evidence_reviewer import _bound_review_schema
from novelty_agent_framework.schemas.domain import ReviewerSupportDraft, ReviewStatus
from test_reviewer_controlled_harness import ChoosingClient, controlled, row
from test_reviewer_failure_classification import local_tool
from test_reviewer_summary_checkpoint import Client, output
from test_novelty_point_reviewer import _request


def allowed(options):
    schema = options.response_format['json_schema']['schema']
    refs = schema['$defs']['RelevantWork']['properties']['evidence_ids']
    feature_refs = schema['$defs']['FeatureComparison']['properties']['evidence_refs']
    assert refs['items'] == feature_refs['items']
    return set(refs['items'].get('enum', []))


def test_no_read_choices_include_only_current_card_evidence():
    request = _request()
    other = request.evidence[0].model_copy(update={'evidence_id': 'unbound-evidence'})
    request = request.model_copy(update={'evidence': [*request.evidence, other]})
    client = ChoosingClient(request, ['可用', '不继续'])
    result = asyncio.run(controlled(client).review_card(request))
    assert result.status.value == 'reviewed'
    assert allowed(client.calls[-1][1]) == set(request.cards[0].evidence_ids)
    assert 'unbound-evidence' not in str(client.calls)


def test_new_read_choices_reach_budget_finalization_and_summary(tmp_path):
    request, tool, _ = local_tool(tmp_path)
    client = ChoosingClient(request, ['可用', '继续阅读正文'])
    reviewer = controlled(client, tool, max_total_read_chars=300)
    reviewer.harness.config = replace(reviewer.harness.config, max_turns=3)
    result = asyncio.run(reviewer.review_card(request))
    assert result.review_evidence
    expected = set(request.cards[0].evidence_ids) | {e.evidence_id for e in result.review_evidence}
    assert allowed(client.calls[-1][1]) == expected
    assert len(client.calls) == 3
    # Use a completed row to exercise summary selection independently of budget fallback.
    result = result.model_copy(update={'status': ReviewStatus.REVIEWED, 'incomplete_reason': None})
    reviewer.model_client = summary_client = Client(output(request, reviewed=True))
    asyncio.run(reviewer.summarize_reviews(request, [row(request, result)]))
    payload = json.loads(summary_client.calls[0][0][1].content)
    displayed = {q['evidence_id'] for r in payload['card_reviews'] for q in r['key_quotes'] if q['quote']}
    assert allowed(summary_client.calls[0][1]) == set(payload['evidence_options']) == displayed
    assert expected == displayed


def test_unissued_reference_repair_uses_same_current_choices():
    request = _request()

    class WrongThenSelect(ChoosingClient):
        async def acomplete(self, messages, *, options=None):
            if messages[0].content.startswith('Repair the format'):
                self.calls.append((messages, options))
                payload = json.loads(messages[1].content)
                assert 'Harness choices' in payload['validation_error']
                draft = json.loads(payload['previous_output'])
                draft['highly_relevant_works'][0]['evidence_ids'] = [next(iter(allowed(options)))]
                return ModelResponse(content=json.dumps(draft))
            response = await super().acomplete(messages, options=options)
            draft = json.loads(response.content)
            if 'support_reason' in draft:
                draft['highly_relevant_works'][0]['evidence_ids'] = ['invented']
            return ModelResponse(content=json.dumps(draft))

    client = WrongThenSelect(request, ['可用', '不继续'])
    result = asyncio.run(controlled(client).review_card(request))
    assert result.status.value == 'reviewed'
    assert len(client.calls) == 4
    assert allowed(client.calls[-1][1]) == allowed(client.calls[-2][1]) == set(request.cards[0].evidence_ids)


def test_empty_choices_forbid_references_without_invalid_empty_enum():
    schema = _bound_review_schema(ReviewerSupportDraft, _request(), {})
    for definition, field in [('RelevantWork', 'evidence_ids'), ('FeatureComparison', 'evidence_refs')]:
        refs = schema['$defs'][definition]['properties'][field]
        assert refs['maxItems'] == 0
        assert 'enum' not in refs['items']
