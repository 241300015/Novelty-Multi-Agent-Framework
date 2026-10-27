"""Caller identity belongs to Harness; inherited quotes do not become new reads."""
import asyncio
import json

import pytest

from backend.env import ModelResponse
from novelty_agent_framework.core.read_authorization import supplied_evidence_for_read
from test_reviewer_fulltext_acquisition import prepare
from test_reviewer_controlled_harness import ChoosingClient, controlled
from test_reviewer_failure_classification import local_tool


class InheritedCitationClient(ChoosingClient):
    def __init__(self, request, *, extend=False):
        super().__init__(request, ["可用", "不继续"])
        self.extend = extend

    async def acomplete(self, messages, *, options=None):
        response = await super().acomplete(messages, options=options)
        data = json.loads(response.content)
        if "support_reason" in data:
            e = self.request.evidence[0]
            data['read_citations'] = [{"read_id": e.provenance['read_id'],
                "char_start": e.locator.char_start,
                "char_end": e.locator.char_end + int(self.extend)}]
        return ModelResponse(content=json.dumps(data))


def test_inherited_quote_remains_input_evidence_without_claiming_reviewer_read(tmp_path):
    request, registry, _, _, events = prepare(tmp_path)
    client = ChoosingClient(request, ["可用", "不继续"])
    review = asyncio.run(controlled(client, registry.get('reader')).review_card(request))
    assert review.status.value == 'reviewed'
    assert not review.read_citations and not review.review_evidence and not review.reader_observations
    assert review.highly_relevant_works[0].evidence_ids == [request.evidence[0].evidence_id]
    assert 'read_id' not in json.dumps([(str(messages), str(options)) for messages, options in client.calls])
    assert review.harness_state['read_receipts'] == [] and events == []


def test_inherited_reference_survives_summary_checkpoint(tmp_path):
    from test_reviewer_summary_checkpoint import Client, output
    from test_reviewer_controlled_harness import row
    request, registry, _, _, _ = prepare(tmp_path)
    reviewer = controlled(ChoosingClient(request, ["可用", "不继续"]), registry.get('reader'))
    review = asyncio.run(reviewer.review_card(request))
    reviewer.model_client = Client(output(request, reviewed=True))
    summary = asyncio.run(reviewer.summarize_with_checkpoint(request, [row(request, review)],
        output_root=tmp_path, run_id='inherited-quote'))
    assert summary.durable and summary.status == 'completed'
    assert summary.review.review_evidence == []


def test_model_cannot_submit_runtime_read_locators(tmp_path):
    request, registry, _, _, _ = prepare(tmp_path)
    review = asyncio.run(controlled(InheritedCitationClient(request, extend=True), registry.get('reader')).review_card(request))
    assert review.status.value == 'insufficient_evidence'
    assert review.execution_issues[-1].code == 'model.output.schema_invalid'


def test_ambiguous_historical_read_alias_requires_explicit_evidence_id(tmp_path):
    request, _, _, _, _ = prepare(tmp_path)
    e = request.evidence[0]
    duplicate = e.model_copy(update={'evidence_id': 'second-evidence'})
    with pytest.raises(ValueError, match='ambiguous'):
        supplied_evidence_for_read([e, duplicate], e.provenance['read_id'])


def test_identical_reader_content_has_distinct_harness_invocation_receipts(tmp_path):
    request, tool, _ = local_tool(tmp_path)
    reviews = [asyncio.run(controlled(ChoosingClient(request,
        ['可用', '继续阅读正文', '可用', '回读有帮助']), tool,
        max_tool_calls=1, max_total_read_chars=300).review_card(request)) for _ in range(2)]
    assert all(r.status.value == 'reviewed' for r in reviews)
    a, b = [r.harness_state['read_receipts'][0] for r in reviews]
    assert a['read_id'] == b['read_id']
    assert a['receipt_id'] != b['receipt_id']
    assert a['caller']['invocation_id'] != b['caller']['invocation_id']
    assert a['caller']['role'] == b['caller']['role'] == 'reviewer'
    assert a['caller']['card_ids'] == [request.cards[0].card_id]
