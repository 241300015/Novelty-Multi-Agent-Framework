"""Runtime owns read locators; the model can cite only displayed Evidence."""
import asyncio
import json

import pytest

from backend.env import ModelResponse
from novelty_agent_framework.agents.evidence_reviewer import _compact_summary_rows
from novelty_agent_framework.core.reviewer_evidence import register_read_evidence
from test_reviewer_controlled_harness import ChoosingClient, controlled, row
from test_reviewer_failure_classification import local_tool
from test_reviewer_summary_checkpoint import Client, output
from test_reviewer_fulltext_acquisition import prepare, run_card


def assert_private(client):
    wire = json.dumps([(str(messages), str(options)) for messages, options in client.calls])
    for field in ('read_id', 'read_citations', 'char_start', 'char_end', 'read_receipts', 'reader_observations'):
        assert field not in wire, field


def test_read_and_summary_use_evidence_while_checkpoint_preserves_runtime_locators(tmp_path):
    request, tool, _ = local_tool(tmp_path)
    client = ChoosingClient(request, ['可用', '继续阅读正文', '可用', '回读有帮助'])
    reviewer = controlled(client, tool, max_tool_calls=1, max_total_read_chars=300)
    review = asyncio.run(reviewer.review_card(request))
    assert review.status.value == 'reviewed'
    assert_private(client)
    evidence, = review.review_evidence
    assert evidence.evidence_id == client.reads[0]['evidence_id']
    assert evidence.exact_quote == client.reads[0]['quote']
    assert evidence.read_id == review.reader_observations[0].read_id
    assert evidence.char_start == 0 and evidence.char_end == 300
    assert review.highly_relevant_works[0].evidence_ids[-1] == evidence.evidence_id
    assert not review.read_citations
    reviewer.model_client = summary_client = Client(output(request, reviewed=True))
    result = asyncio.run(reviewer.summarize_with_checkpoint(request, [row(request, review)],
        output_root=tmp_path, run_id='evidence-projection'))
    assert result.durable and result.status == 'completed'
    assert result.review.review_evidence == review.review_evidence
    assert_private(summary_client)


def test_acquisition_then_read_never_exposes_runtime_locators(tmp_path):
    request, registry, _, _, _ = prepare(tmp_path)
    review, _, client = run_card(request, registry,
        ['待核验', '尝试回读本地材料', '在线获取', '读取新获取', '可用', '回读有帮助'])
    assert len(review.review_evidence) == 2
    assert_private(client)


def test_registered_read_does_not_automatically_become_support_for_a_claim(tmp_path):
    request, tool, _ = local_tool(tmp_path)

    class NoCitations(ChoosingClient):
        async def acomplete(self, messages, *, options=None):
            response = await super().acomplete(messages, options=options)
            draft = json.loads(response.content)
            if 'support_reason' in draft:
                draft['highly_relevant_works'] = []
            return ModelResponse(content=json.dumps(draft))

    client = NoCitations(request, ['可用', '继续阅读正文', '可用', '回读有帮助'])
    review = asyncio.run(controlled(client, tool, max_tool_calls=1, max_total_read_chars=300).review_card(request))
    assert review.status.value == 'reviewed' and review.review_evidence
    assert not review.highly_relevant_works and not review.feature_comparisons
    rows, _ = _compact_summary_rows(request, [row(request, review)])
    assert all(q['semantic_support'] == 'unassessed' and not q['cited_by_card'] for q in rows[0]['key_quotes'])


@pytest.mark.parametrize('tamper', ['hash', 'work', 'range'])
def test_registration_rejects_unbound_or_inconsistent_reader_results(tmp_path, tamper):
    request, tool, _ = local_tool(tmp_path)
    from novelty_agent_framework.schemas import ReaderArguments
    entry = next(e for e in tool.material_catalog(request) if e['role'] == 'extracted_text')
    observation = asyncio.run(tool.ainvoke(ReaderArguments(artifact_id=entry['artifact_id'],
        char_start=0, max_chars=300), scope=request))
    raw = dict(observation.payload['read_result'])
    if tamper == 'hash':
        raw['sha256'] = 'wrong'
    elif tamper == 'work':
        raw['work_id'] = 'other-work'
    else:
        raw['char_end'] += 1
    with pytest.raises(ValueError):
        register_read_evidence(raw, request, tool.material_catalog(request))
