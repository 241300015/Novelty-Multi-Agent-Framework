"""Summary recovery on existing historical Artifacts; no model or network service."""
import asyncio
import hashlib
import json
from dataclasses import replace
from datetime import datetime, timezone

import pytest
from backend.env import ModelResponse, ModelToolCall
from novelty_agent_framework.agents import NoveltyEvidenceReviewer
from novelty_agent_framework.agents.reviewer_checkpoint import ReviewerSummaryCheckpoint, digest
from novelty_agent_framework.schemas import NoveltyPointReview, ReaderArguments
from novelty_agent_framework.tools import ResearcherToolRegistry
from test_reviewer_failure_classification import local_tool
from test_reviewer_evidence_repair import BODY_ID


class Client:
    def __init__(self, *responses):
        self.responses, self.calls = list(responses), []
    async def acomplete(self, messages, *, options=None):
        self.calls.append((messages, options))
        value = self.responses.pop(0)
        if isinstance(value, BaseException):
            raise value
        return value


def output(request, *, reviewed=False):
    value = {'novelty_point_id':request.novelty_point.point_id,
             'status':'reviewed' if reviewed else 'insufficient_evidence'}
    if reviewed:
        value.update(verdict='partially_novel', verdict_reason='Test only: limited supported relationship', confidence=0.5)
    else:
        value['supplement_request'] = {'reason':'Test only: remaining features require source comparison'}
    value['highly_relevant_works'] = [{'work_id':request.evidence[0].work_id,
        'card_ids':[request.cards[0].card_id], 'evidence_ids':[request.evidence[0].evidence_id],
        'relevance_reason':'Test only: retain original cited fact'}]
    return ModelResponse(content=json.dumps(value))


def setup_case(tmp_path, *, incremental=False):
    request, tool, path = local_tool(tmp_path)
    card = json.loads(output(request).content)
    responses = []
    if incremental:
        args = ReaderArguments(artifact_id=BODY_ID, char_start=16000, max_chars=300)
        read = asyncio.run(tool.ainvoke(args, scope=request)).payload['read_result']
        card['read_citations'] = [{'read_id':read['read_id'],'char_start':16003,'char_end':16100}]
        responses.append(ModelResponse(content=None, tool_calls=(ModelToolCall(id='read',name='reader',arguments=args.model_dump()),)))
    responses.append(ModelResponse(content=json.dumps(card)))
    client = Client(*responses, TimeoutError('injected summary transport timeout'), output(request, reviewed=True))
    reviewer = NoveltyEvidenceReviewer(client, tool_registry=ResearcherToolRegistry([tool]))
    review = asyncio.run(reviewer.review_card(request))
    assert review.incomplete_reason == 'semantic_evidence'
    rows = [{'index':1,'card_id':request.cards[0].card_id,'novelty_point_id':request.novelty_point.point_id,
             'status':'completed','review':review.model_dump(mode='json')}]
    return request, reviewer, client, rows, path


@pytest.mark.parametrize('incremental',[False,True])
def test_failed_summary_retains_card_facts_and_recovers_only_summary(tmp_path, incremental):
    request, reviewer, client, rows, _ = setup_case(tmp_path, incremental=incremental)
    initial = asyncio.run(reviewer.summarize_with_checkpoint(request, rows, output_root=tmp_path, run_id='run-A'))
    assert initial.status == 'failed' and initial.durable
    assert initial.review.incomplete_reason == 'technical_error'
    assert initial.partial_card_results == rows
    assert initial.execution_issues[0].code == 'model.timeout'
    saved = next(tmp_path.rglob('checkpoint.json'))
    before = saved.read_bytes()
    calls = len(client.calls)
    recovered = asyncio.run(reviewer.recover_summary(request, output_root=tmp_path,
        run_id='run-A', checkpoint_id=initial.checkpoint_id))
    assert len(client.calls) == calls + 1
    assert client.calls[-1][1].tools == () and client.calls[-1][1].tool_choice == 'none'
    assert recovered.status == 'completed' and recovered.review.status.value == 'reviewed'
    assert recovered.partial_card_results == rows
    assert saved.read_bytes() == before
    assert recovered.review.highly_relevant_works[0].evidence_ids == [request.evidence[0].evidence_id]
    if incremental:
        assert recovered.review.review_evidence[0].exact_quote == rows[0]['review']['review_evidence'][0]['exact_quote']
    with pytest.raises(FileExistsError):
        asyncio.run(reviewer.recover_summary(request, output_root=tmp_path, run_id='run-A', checkpoint_id=initial.checkpoint_id))
    assert len(client.calls) == calls + 1


@pytest.mark.parametrize('mutation',['artifact','scope','run','config','checkpoint','read_ledger','read_range','quote','work'])
def test_recovery_rejects_changed_scope_or_source_before_model(tmp_path, mutation):
    request, reviewer, client, rows, path = setup_case(tmp_path, incremental=True)
    initial = asyncio.run(reviewer.summarize_with_checkpoint(request, rows, output_root=tmp_path, run_id='run-A'))
    assert initial.durable
    checkpoint = next(tmp_path.rglob('checkpoint.json'))
    run_id = 'run-A'
    if mutation == 'artifact':
        path.write_text('changed')
    elif mutation == 'scope':
        request = request.model_copy(deep=True)
        request.novelty_point.claim += 'different request'
    elif mutation == 'run':
        run_id = 'run-B'
    elif mutation == 'config':
        reviewer.config = replace(reviewer.config, temperature=0.1)
    else:
        payload = json.loads(checkpoint.read_text())
        review = payload['card_results'][0]['review']
        if mutation == 'checkpoint':
            payload['summary_input_date'] = '2000-01-01'  # Leave checksum stale.
        elif mutation == 'read_ledger':
            review['reader_observations'] = []
        elif mutation == 'read_range':
            review['reader_observations'][0]['char_end'] -= 1
        elif mutation == 'quote':
            review['review_evidence'][0]['exact_quote'] = 'fabricated unsupported text'
        elif mutation == 'work':
            review['review_evidence'][0]['work_id'] = 'foreign-work'
        if mutation != 'checkpoint':
            payload.pop('content_sha256')
            payload['content_sha256'] = digest(payload)
        checkpoint.write_text(json.dumps(payload))
    calls = len(client.calls)
    with pytest.raises((ValueError, OSError)):
        asyncio.run(reviewer.recover_summary(request, output_root=tmp_path, run_id=run_id, checkpoint_id=initial.checkpoint_id))
    assert len(client.calls) == calls


def test_checkpoint_failure_does_not_claim_durable_or_erase_valid_summary(tmp_path, monkeypatch):
    request, reviewer, client, rows, _ = setup_case(tmp_path)
    client.responses = [output(request, reviewed=True)]
    from novelty_agent_framework.agents import reviewer_checkpoint as module
    monkeypatch.setattr(module, '_atomic_write_json', lambda *args: (_ for _ in ()).throw(OSError('disk full')))
    result = asyncio.run(reviewer.summarize_with_checkpoint(request, rows, output_root=tmp_path, run_id='run-A'))
    assert result.status == 'checkpoint_unavailable' and not result.durable
    assert result.checkpoint_id is None
    assert result.review.status.value == 'reviewed'
    assert result.partial_card_results == rows
    assert not list(tmp_path.rglob('checkpoint.json'))


def test_duplicate_or_cross_card_rows_are_not_recoverable(tmp_path):
    request, reviewer, client, rows, _ = setup_case(tmp_path)
    with pytest.raises(ValueError, match='exactly one row'):
        ReviewerSummaryCheckpoint.create(reviewer, request, rows+rows, output_root=tmp_path, run_id='run-A')
    rows[0]['novelty_point_id'] = 'NP-foreign'
    with pytest.raises(ValueError, match='outside request scope'):
        ReviewerSummaryCheckpoint.create(reviewer, request, rows, output_root=tmp_path, run_id='run-A')


def test_concurrent_explicit_recoveries_reserve_only_one_attempt(tmp_path):
    request, reviewer, client, rows, _ = setup_case(tmp_path)
    initial = asyncio.run(reviewer.summarize_with_checkpoint(request, rows, output_root=tmp_path, run_id='run-A'))
    async def concurrent():
        return await asyncio.gather(*(reviewer.recover_summary(request, output_root=tmp_path,
            run_id='run-A', checkpoint_id=initial.checkpoint_id) for _ in range(2)), return_exceptions=True)
    calls = len(client.calls)
    outcomes = asyncio.run(concurrent())
    assert sum(isinstance(item, FileExistsError) for item in outcomes) == 1
    assert len(client.calls) == calls+1


def test_failed_retry_preserves_initial_snapshot_and_original_failure(tmp_path):
    request, reviewer, client, rows, _ = setup_case(tmp_path)
    initial = asyncio.run(reviewer.summarize_with_checkpoint(request, rows, output_root=tmp_path, run_id='run-A'))
    client.responses = [TimeoutError('second injected timeout')]
    result = asyncio.run(reviewer.recover_summary(request, output_root=tmp_path, run_id='run-A', checkpoint_id=initial.checkpoint_id))
    assert result.status == 'failed' and result.review.verdict is None
    assert result.partial_card_results == initial.partial_card_results
    persisted = json.loads(next(tmp_path.rglob('initial/result.json')).read_text())
    assert persisted['execution_issues'][0]['code'] == 'model.timeout'


def test_recovery_cannot_reset_shared_summary_deadline(tmp_path):
    request, reviewer, client, rows, _ = setup_case(tmp_path)
    initial = asyncio.run(reviewer.summarize_with_checkpoint(
        request, rows, output_root=tmp_path, run_id='run-A'))
    checkpoint = next(tmp_path.rglob('checkpoint.json'))
    payload = json.loads(checkpoint.read_text())
    payload['summary_deadline_at'] = datetime(2000, 1, 1, tzinfo=timezone.utc).isoformat()
    payload.pop('content_sha256')
    payload['content_sha256'] = digest(payload)
    checkpoint.write_text(json.dumps(payload))
    calls = len(client.calls)
    recovered = asyncio.run(reviewer.recover_summary(
        request, output_root=tmp_path, run_id='run-A',
        checkpoint_id=initial.checkpoint_id))
    assert len(client.calls) == calls
    assert recovered.status == 'failed'
    assert recovered.review.incomplete_reason == 'budget_exhausted'


def test_completed_semantic_insufficiency_cannot_be_resampled_as_recovery(tmp_path):
    request, reviewer, client, rows, _ = setup_case(tmp_path)
    client.responses = [output(request)]
    result = asyncio.run(reviewer.summarize_with_checkpoint(request, rows, output_root=tmp_path, run_id='run-A'))
    assert result.status == 'completed'
    calls = len(client.calls)
    with pytest.raises(ValueError, match='already completed'):
        asyncio.run(reviewer.recover_summary(request, output_root=tmp_path, run_id='run-A', checkpoint_id=result.checkpoint_id))
    assert len(client.calls) == calls


def test_result_write_failure_never_returns_durable_success(tmp_path, monkeypatch):
    request, reviewer, client, rows, _ = setup_case(tmp_path)
    checkpoint = ReviewerSummaryCheckpoint.create(reviewer, request, rows, output_root=tmp_path, run_id='run-A')
    from novelty_agent_framework.agents import reviewer_checkpoint as module
    original = module._atomic_write_json
    def write(path, value):
        if path.name == 'result.json':
            raise OSError('disk full')
        original(path, value)
    monkeypatch.setattr(module, '_atomic_write_json', write)
    result = asyncio.run(checkpoint.attempt(reviewer))
    assert not result.durable and result.status == 'failed'
    assert checkpoint.path.exists()
    assert result.partial_card_results == rows


def test_cancelled_summary_keeps_checkpoint_and_can_recover(tmp_path):
    request, reviewer, client, rows, _ = setup_case(tmp_path)
    client.responses = [asyncio.CancelledError(), output(request)]
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(reviewer.summarize_with_checkpoint(request, rows, output_root=tmp_path, run_id='run-A'))
    payload = json.loads(next(tmp_path.rglob('checkpoint.json')).read_text())
    state = json.loads(next(tmp_path.rglob('initial/state.json')).read_text())
    assert state['status'] == 'cancelled' and state['error_type'] == 'CancelledError'
    result = asyncio.run(reviewer.recover_summary(request, output_root=tmp_path, run_id='run-A',
                                                checkpoint_id=payload['checkpoint_id']))
    assert result.status == 'completed' and result.review.verdict is None
    assert result.partial_card_results == rows
