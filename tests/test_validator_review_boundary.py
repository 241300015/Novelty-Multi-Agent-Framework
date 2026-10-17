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


@pytest.mark.parametrize('confidence,relevance', [(0.0, 0.0), (0.1, 0.9), (0.9, 0.1), (1.0, 1.0)])
def test_validator_does_not_filter_by_model_scores(tmp_path, confidence, relevance):
    store, _ = _store(tmp_path)
    card = _card(confidence=confidence, relevance=relevance)
    result = validate_cards(store, [card])
    assert result.accepted == (card,) and not result.rejected
    assert result.integrity['validation_passed']


@pytest.mark.parametrize('field', ['confidence', 'relevance'])
@pytest.mark.parametrize('value', [-0.1, 1.1, float('nan')])
def test_validator_still_rejects_invalid_score_fields(tmp_path, field, value):
    store, _ = _store(tmp_path)
    card = _card().model_copy(update={field: value})
    result = validate_cards(store, [card])
    assert not result.accepted and result.rejected
    assert not result.integrity['validation_passed']


def test_validator_low_scores_do_not_bypass_file_integrity(tmp_path):
    store, path = _store(tmp_path)
    path.write_text('tampered')
    result = validate_cards(store, [_card(confidence=0.0, relevance=0.0)])
    assert not result.accepted and result.rejected
    assert result.integrity['provenance_rejected_card_ids'] == ['card-1']


@pytest.mark.parametrize('url,accepted', [
    ('https://unrelated.example/other-paper', False),
    ('https://example.test/work-1?different=1', False),
    ('https://example.test/WORK-1', False),
    ('https://example.test:invalid/work-1', False),
    ('file:///tmp/paper.txt', False),
    ('https://user@example.test/work-1', False),
    ('https://EXAMPLE.TEST:443/work-1', True),
])
def test_validator_checks_source_url_against_bound_metadata(tmp_path, url, accepted):
    store, _ = _store(tmp_path)
    card = _card()
    card.sources[0].url = url
    result = validate_cards(store, [card])
    assert bool(result.accepted) is accepted
    if not accepted:
        assert 'URL' in result.rejected[0][1]


@pytest.mark.parametrize('stored_doi,claimed_doi,accepted', [
    (None, '10.1234/invented', False),
    ('10.1234/real', '10.1234/other', False),
    ('10.1234/real', 'https://doi.org/10.1234/REAL', True),
    ('10.1234/real', 'DOI:10.1234/real', True),
    ('10.1234/real', '', False),
])
def test_validator_checks_claimed_doi_even_without_local_doi(tmp_path, stored_doi, claimed_doi, accepted):
    from novelty_agent_framework.schemas.references import ExternalIdentifier
    store, _ = _store(tmp_path)
    manifest = store.load_manifest('paper-1')
    if stored_doi:
        manifest.works[0].identifiers.append(ExternalIdentifier(namespace='doi', value=stored_doi))
        store.persist_manifest('paper-1', manifest)
    card = _card()
    card.sources[0].doi = claimed_doi
    result = validate_cards(store, [card])
    assert bool(result.accepted) is accepted
    if not accepted:
        assert 'DOI' in result.rejected[0][1]


def test_validator_rejects_url_without_local_metadata(tmp_path):
    store, _ = _store(tmp_path)
    manifest = store.load_manifest('paper-1')
    manifest.works[0].identifiers = []
    store.persist_manifest('paper-1', manifest)
    result = validate_cards(store, [_card()])
    assert not result.accepted
    assert 'URL cannot be verified' in result.rejected[0][1]


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
    assert sanitized.execution_issues[0].code.value == {
        'missing': 'gate_a.review.missing', 'duplicate': 'gate_a.review.duplicate',
        'foreign_card': 'reviewer.reference.unknown_card',
        'foreign_evidence': 'reviewer.reference.unknown_evidence',
        'foreign_work': 'reviewer.reference.work_mismatch',
        'foreign_feature': 'reviewer.comparison.unknown_feature',
        'not_novel': 'gate_a.verdict.coverage_incomplete',
        'no_cards': 'reviewer.reference.unknown_card',
        'incomplete_reviewed': 'gate_a.review.status_conflict',
        'schema': 'model.output.schema_invalid',
    }[damage]
    assert sanitized.execution_issues[0].scope.node == 'gate_a.review'
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


@pytest.mark.parametrize('damage', [
    'missing_locator', 'missing_location', 'both_missing', 'blank_location',
    'section_only', 'missing_end', 'wrong_artifact', 'wrong_start', 'wrong_end',
    'free_text_location', 'no_evidence', 'unpaired_source', 'unpaired_evidence',
    'matching_but_wrong_original_span', 'wrong_quote',
])
def test_validator_requires_complete_matching_locations(tmp_path, damage):
    from novelty_agent_framework.schemas import EvidenceLocator
    store, _ = _store(tmp_path)
    card, evidence = _card(), [_evidence()]
    if damage in {'missing_locator', 'both_missing'}:
        evidence[0].locator = None
    if damage in {'missing_location', 'both_missing'}:
        card.sources[0].location = None
    if damage == 'blank_location':
        card.sources[0].location = '  '
    if damage == 'section_only':
        evidence[0].locator = EvidenceLocator(section='Introduction')
    if damage == 'missing_end':
        evidence[0].locator = EvidenceLocator(char_start=7)
    if damage == 'wrong_artifact':
        card.sources[0].location = 'artifact other chars:7-27'
    if damage == 'wrong_start':
        card.sources[0].location = 'artifact artifact-1 chars:8-27'
    if damage == 'wrong_end':
        card.sources[0].location = 'artifact artifact-1 chars:7-26'
    if damage == 'free_text_location':
        card.sources[0].location = 'Section 1'
    if damage == 'no_evidence':
        evidence = []
    if damage == 'unpaired_source':
        card.sources.append(card.sources[0].model_copy(update={'location': None}))
    if damage == 'unpaired_evidence':
        evidence.append(_evidence(evidence_id='ev-2', locator=EvidenceLocator(char_start=0, char_end=6), quote='prefix'))
        card.evidence_ids.append('ev-2')
    if damage == 'matching_but_wrong_original_span':
        evidence[0].locator = EvidenceLocator(char_start=0, char_end=6)
        card.sources[0].location = 'artifact artifact-1 chars:0-6'
    if damage == 'wrong_quote':
        card.sources[0].quote = 'prefix'
    result = validate_cards(store, [card], evidence)
    assert not result.accepted and result.rejected
    assert not result.integrity['validation_passed']


def test_production_validator_location_gate_cannot_be_disabled(tmp_path):
    from novelty_agent_framework.agents import EvidenceValidationConfig
    store, _ = _store(tmp_path)
    card = _card()
    card.sources[0].location = None
    validator = DefaultEvidenceValidator(EvidenceValidationConfig(require_source_location=False))
    result = validator.validate([card], tasks=[_task()], evidence=[_evidence()],
        novelty_points=[_point()], paper_id='paper-1', reference_store=store)
    assert not result.accepted and result.rejected
