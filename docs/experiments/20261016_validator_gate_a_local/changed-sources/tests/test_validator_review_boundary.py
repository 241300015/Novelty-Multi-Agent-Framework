"""Validator owns Cards; Gate A owns Reviews and cannot invalidate Cards."""
import asyncio
import json

import pytest

from novelty_agent_framework.agents.evidence_validator import DefaultEvidenceValidator
from novelty_agent_framework.core.integrity_gates import validate_review_integrity
from novelty_agent_framework.persistence import ReferenceStore
from novelty_agent_framework.schemas import NoveltyPointReview, PaperInput, RelevantWork
from novelty_agent_framework.workflows import NoveltyWorkflow
from test_integrity_gates import _card, _evidence, _point, _store, _task
from test_reviewer_summary_checkpoint import setup_case, output


def validate_cards(store, cards, evidence=None):
    return DefaultEvidenceValidator().validate(cards, tasks=[_task()],
        evidence=[_evidence()] if evidence is None else evidence,
        novelty_points=[_point()], paper_id='paper-1', reference_store=store)


def review(**changes):
    values = dict(novelty_point_id='NP-1', status='reviewed', verdict='partially_novel',
        verdict_reason='Supported fixture comparison', confidence=0.8,
        highly_relevant_works=[RelevantWork(work_id='work-1', card_ids=['card-1'],
            evidence_ids=['ev-1'], relevance_reason='fixture')])
    values.update(changes)
    return NoveltyPointReview(**values)


def gate(store, reviews, **kwargs):
    values = dict(evidence_cards=[_card()], evidence=[_evidence()], tasks=[_task()],
        novelty_points=[_point()], paper_id='paper-1', reference_store=store)
    values.update(kwargs)
    return validate_review_integrity(reviews, **values)


def test_validator_preserves_same_title_cards_and_rejects_duplicate_ids(tmp_path):
    store, _ = _store(tmp_path)
    cards = [_card(), _card(card_id='card-2')]
    result = validate_cards(store, cards)
    assert result.accepted == tuple(cards) and not result.rejected
    duplicate = validate_cards(store, [cards[0], cards[0]])
    assert not duplicate.accepted and len(duplicate.rejected) == 2


@pytest.mark.parametrize('damage', ['artifact', 'title', 'source_title', 'quote', 'source_record', 'ambiguous'])
def test_validator_rejects_broken_provenance_and_metadata(tmp_path, damage):
    store, path = _store(tmp_path)
    card, evidence = _card(), [_evidence()]
    if damage == 'artifact':
        path.write_text('tampered')
    elif damage == 'title':
        card.document_title = 'Another work'
    elif damage == 'source_title':
        card.sources[0].title = 'Another work'
    elif damage == 'quote':
        card.sources[0].quote = 'Invented quote'
    elif damage == 'source_record':
        evidence[0].provenance = {'source_record_id': 'missing-source'}
    elif damage == 'ambiguous':
        evidence.append(_evidence(work_id='other-work'))
    result = validate_cards(store, [card], evidence)
    assert not result.accepted and result.rejected
    assert not result.integrity['validation_passed']


def test_validator_quality_rejection_is_in_audit(tmp_path):
    store, _ = _store(tmp_path)
    result = validate_cards(store, [_card(confidence=0.1)])
    assert not result.integrity['validation_passed']
    assert result.integrity['checked_card_count'] == result.integrity['rejected_card_count'] == 1


def test_gate_a_accepts_review_and_valid_insufficient_state(tmp_path):
    store, _ = _store(tmp_path)
    valid = review()
    assert gate(store, [valid]).reviews == (valid,)
    assert gate(store, [valid]).audit()['validation_passed']
    insufficient = NoveltyPointReview(novelty_point_id='NP-1', status='insufficient_evidence',
                                      incomplete_reason='semantic_evidence')
    assert gate(store, [insufficient]).audit()['validation_passed']


@pytest.mark.parametrize('damage', ['missing', 'duplicate', 'foreign_card', 'foreign_evidence',
    'foreign_work', 'foreign_feature', 'not_novel', 'no_cards', 'schema', 'incomplete_reviewed'])
def test_gate_a_replaces_invalid_verdict_with_technical_failure(tmp_path, damage):
    store, _ = _store(tmp_path)
    value = review()
    reviews, extra = [value], {}
    if damage == 'missing':
        reviews = []
    elif damage == 'duplicate':
        reviews.append(value.model_copy(deep=True))
    elif damage == 'foreign_card':
        value.highly_relevant_works[0].card_ids = ['foreign']
    elif damage == 'foreign_evidence':
        value.highly_relevant_works[0].evidence_ids = ['foreign']
    elif damage == 'foreign_work':
        value.highly_relevant_works[0].work_id = 'foreign'
    elif damage == 'foreign_feature':
        from novelty_agent_framework.schemas.domain import FeatureComparison
        value.feature_comparisons = [FeatureComparison(feature_id='F99', work_id='work-1',
            relation='supported', basis_type='direct_statement', evidence_refs=['ev-1'], reason='fixture')]
    elif damage == 'not_novel':
        value = review(verdict='not_novel')
        reviews = [value]
    elif damage == 'no_cards':
        extra['evidence_cards'] = []
    elif damage == 'schema':
        value.confidence = 2
    elif damage == 'incomplete_reviewed':
        value.incomplete_reason = 'technical_error'
    result = gate(store, reviews, **extra)
    assert not result.audit()['validation_passed']
    assert len(result.reviews) == 1
    sanitized = result.reviews[0]
    assert sanitized.status.value == 'insufficient_evidence' and sanitized.verdict is None
    assert sanitized.incomplete_reason == 'technical_error'
    assert sanitized.execution_issues[0].code.value == 'model.output_reference'
    assert result.rejected[0]['reviews'] == [x.model_dump(mode='json') for x in reviews]


def test_unknown_review_is_audited_without_displacing_valid_point(tmp_path):
    store, _ = _store(tmp_path)
    valid, foreign = review(), review(novelty_point_id='NP-other')
    result = gate(store, [valid, foreign])
    assert result.reviews == (valid,)
    assert result.rejected[0]['novelty_point_id'] == 'NP-other'


@pytest.mark.parametrize('damage', [None, 'missing_ledger', 'quote', 'hash', 'origin_card', 'file'])
def test_gate_a_checks_reviewer_registered_reads(tmp_path, damage):
    request, _, _, rows, path = setup_case(tmp_path, incremental=True)
    value = NoveltyPointReview.model_validate(rows[0]['review'])
    if damage == 'missing_ledger':
        value.reader_observations = []
    elif damage == 'quote':
        value.review_evidence[0].exact_quote = 'fabricated quote'
    elif damage == 'hash':
        value.reader_observations[0].text_sha256 = '0' * 64
    elif damage == 'origin_card':
        value.review_evidence[0].origin_card_id = 'foreign'
    elif damage == 'file':
        path.write_text('tampered')
    result = validate_review_integrity([value], evidence_cards=request.cards,
        evidence=request.evidence, tasks=request.tasks, novelty_points=[request.novelty_point],
        paper_id=request.subject_paper_id, reference_store=ReferenceStore(tmp_path))
    assert result.audit()['validation_passed'] is (damage is None)


def test_summary_carries_reads_needed_by_gate_a(tmp_path):
    request, reviewer, client, rows, _ = setup_case(tmp_path, incremental=True)
    client.responses = [output(request, reviewed=True)]
    value = asyncio.run(reviewer.summarize_reviews(request, rows))
    assert value.reader_observations
    assert value.review_evidence
    result = validate_review_integrity([value], evidence_cards=request.cards,
        evidence=request.evidence, tasks=request.tasks, novelty_points=[request.novelty_point],
        paper_id=request.subject_paper_id, reference_store=ReferenceStore(tmp_path))
    assert result.audit()['validation_passed'], result.audit()


def test_workflow_persists_cards_before_review_and_gate_a_preserves_them(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    store, path = _store(tmp_path / 'outputs')
    workflow = NoveltyWorkflow.default()
    workflow.reference_store = store
    paper = PaperInput(paper_id='paper-1', title='Paper', full_text='body')
    state = dict(paper=paper, novelty_points=[_point()], all_research_tasks=[_task()],
        raw_evidence=[_evidence()], raw_evidence_cards=[_card()])
    validated = asyncio.run(workflow._validate_evidence(state))
    assert validated['evidence_cards']
    saved_cards = (tmp_path / 'outputs/paper-1/evidence-cards.json').read_bytes()
    invalid = review()
    invalid.highly_relevant_works[0].evidence_ids = ['foreign']
    rows = [{'card_id': 'card-1', 'status': 'completed', 'review': review().model_dump(mode='json')}]
    gated = asyncio.run(workflow._validate_synthesis_input({**state, **validated,
        'novelty_reviews': [invalid], 'review_card_results': rows}))
    assert 'evidence_cards' not in gated and 'rejected_evidence' not in gated
    assert (tmp_path / 'outputs/paper-1/evidence-cards.json').read_bytes() == saved_cards
    saved = json.loads((tmp_path / 'outputs/paper-1/novelty-reviews.json').read_text())
    assert saved['card_reviews'] == rows
    assert saved['reviews'][0]['verdict'] is None
    assert saved['integrity']['rejected_reviews'][0]['reviews'][0]['verdict'] == 'partially_novel'
    path.unlink()
    rejected = asyncio.run(workflow._validate_evidence(state))
    assert not rejected['evidence_cards']
    # An explicitly empty Validator result must not fall back to stale Cards.
    output_state = asyncio.run(workflow._review_evidence({**state, **rejected, 'evidence_cards': [_card()]}))
    assert output_state['evidence_cards'] == []
