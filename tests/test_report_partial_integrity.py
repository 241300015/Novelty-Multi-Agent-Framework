"""Gate B audits partial results against real local historical artifacts; no service."""
from copy import deepcopy

import pytest

from novelty_agent_framework.core.integrity_gates import validate_report_integrity
from novelty_agent_framework.persistence import ReferenceStore
from novelty_agent_framework.schemas import (
    NoveltyConclusion, NoveltyPointReview, NoveltyReport,
)
from novelty_agent_framework.schemas.domain import FeatureComparison, PartialCardReview
from test_reviewer_summary_checkpoint import setup_case


def case(tmp_path, *, incremental=True):
    request, _, _, rows, path = setup_case(tmp_path, incremental=incremental)
    review = NoveltyPointReview.model_validate(rows[0]['review'])
    if incremental:
        review.highly_relevant_works[0].evidence_ids.append(review.review_evidence[0].evidence_id)
        review.feature_comparisons = [FeatureComparison(
            feature_id='F1', work_id=request.evidence[0].work_id, relation='partially_supported',
            basis_type='direct_statement', reason='Fixture citation binding, not semantic assessment',
            evidence_refs=[request.evidence[0].evidence_id, review.review_evidence[0].evidence_id])]
    summary = NoveltyPointReview(novelty_point_id=request.novelty_point.point_id,
        status='insufficient_evidence', incomplete_reason='technical_error')
    report = NoveltyReport(paper_id=request.subject_paper_id,
        conclusions=[NoveltyConclusion(novelty_point_id=request.novelty_point.point_id,
            review_status=summary.status, incomplete_reason=summary.incomplete_reason, summary='Summary failed')],
        partial_card_reviews=[PartialCardReview(card_id=request.cards[0].card_id,
            novelty_point_id=request.novelty_point.point_id, review=review, summary_status='failed')])
    kwargs = dict(novelty_points=[request.novelty_point], evidence_cards=request.cards,
        novelty_reviews=[summary], evidence=request.evidence, reference_store=ReferenceStore(tmp_path))
    return report, kwargs, path


@pytest.mark.parametrize('incremental', [False, True])
def test_partial_closure_accepts_original_and_registered_reader_evidence(tmp_path, incremental):
    report, kwargs, _ = case(tmp_path, incremental=incremental)
    before = report.model_dump(mode='json')
    result = validate_report_integrity(report, **kwargs)
    assert result.validation_passed, result.issues
    assert result.audit()['checked_partial_card_count'] == 1
    assert report.model_dump(mode='json') == before


@pytest.mark.parametrize('damage', ['removed_card', 'cross_point', 'duplicate_row', 'original_missing',
    'original_cross_work', 'foreign_card_ref', 'unknown_evidence', 'incremental_cross_work',
    'incremental_other_card', 'missing_ledger', 'ledger_work', 'ledger_hash', 'ledger_range',
    'incremental_quote', 'incremental_id', 'source_record', 'file_hash', 'missing_store'])
def test_partial_closure_rejects_broken_references_without_mutation(tmp_path, damage):
    report, kwargs, path = case(tmp_path)
    row = report.partial_card_reviews[0]
    review = row.review
    if damage == 'removed_card':
        kwargs['evidence_cards'] = []
    elif damage == 'cross_point':
        kwargs['evidence_cards'] = [kwargs['evidence_cards'][0].model_copy(update={'novelty_point_id':'NP-other'})]
    elif damage == 'duplicate_row':
        report.partial_card_reviews.append(deepcopy(row))
    elif damage == 'original_missing':
        kwargs['evidence'] = []
    elif damage == 'original_cross_work':
        kwargs['evidence'] = [kwargs['evidence'][0].model_copy(update={'work_id':'foreign'})]
    elif damage == 'foreign_card_ref':
        review.highly_relevant_works[0].card_ids = ['card-foreign']
    elif damage == 'unknown_evidence':
        review.feature_comparisons[0].evidence_refs = ['ev-foreign']
    elif damage == 'incremental_cross_work':
        review.review_evidence[0].work_id = 'foreign'
    elif damage == 'incremental_other_card':
        review.review_evidence[0].origin_card_id = 'card-foreign'
    elif damage == 'missing_ledger':
        review.reader_observations = []
    elif damage == 'ledger_work':
        review.reader_observations[0].work_id = 'foreign'
    elif damage == 'ledger_hash':
        review.reader_observations[0].text_sha256 = 'f' * 64
    elif damage == 'ledger_range':
        review.reader_observations[0].char_end -= 1
    elif damage == 'incremental_quote':
        review.review_evidence[0].exact_quote = 'Unsupported invented quotation'
    elif damage == 'incremental_id':
        replacement = 'rev_ev_forged'
        old = review.review_evidence[0].evidence_id
        review.review_evidence[0].evidence_id = replacement
        review.highly_relevant_works[0].evidence_ids = [replacement if x == old else x for x in review.highly_relevant_works[0].evidence_ids]
        review.feature_comparisons[0].evidence_refs = [replacement if x == old else x for x in review.feature_comparisons[0].evidence_refs]
    elif damage == 'source_record':
        review.review_evidence[0].source_record_id = 'source-foreign'
    elif damage == 'file_hash':
        path.write_text('tampered artifact')
    elif damage == 'missing_store':
        kwargs.pop('reference_store')
    before = report.model_dump(mode='json')
    result = validate_report_integrity(report, **kwargs)
    assert not result.validation_passed
    assert any('partial card review' in issue for issue in result.issues), result.issues
    assert report.model_dump(mode='json') == before


def test_partial_closure_accepts_unrelated_empty_eof_observation(tmp_path):
    import hashlib
    from novelty_agent_framework.schemas.domain import ReviewerReadAudit

    report, kwargs, path = case(tmp_path)
    review = report.partial_card_reviews[0].review
    artifact_id = review.review_evidence[0].artifact_id
    read = kwargs['reference_store'].read_document_slice(report.paper_id, artifact_id=artifact_id,
        char_start=len(path.read_text(encoding='utf-8')), max_chars=1)
    assert read.char_start == read.char_end and not read.text
    review.reader_observations.append(ReviewerReadAudit(read_id=read.read_id,
        namespace=read.namespace.value, work_id=read.work_id, artifact_id=read.artifact_id,
        artifact_hash=read.sha256, char_start=read.char_start, char_end=read.char_end,
        text_sha256=hashlib.sha256(read.text.encode()).hexdigest()))
    result = validate_report_integrity(report, **kwargs)
    assert result.validation_passed, result.issues


def test_partial_closure_is_checked_even_when_conclusion_has_no_card_references(tmp_path):
    report, kwargs, _ = case(tmp_path)
    assert not report.conclusions[0].supporting_card_ids
    kwargs['evidence_cards'] = []
    result = validate_report_integrity(report, **kwargs)
    assert not result.validation_passed
    assert result.audit()['checked_partial_card_count'] == 1
    assert result.audit()['matched_count'] == 1  # Authoritative point conclusion still matches.


def test_gate_b_checks_inherited_work_cards_even_when_review_matches(tmp_path):
    from novelty_agent_framework.core.report_binding import assemble_report_from_draft
    from novelty_agent_framework.schemas import ReportNarrativeDraft

    report, kwargs, _ = case(tmp_path, incremental=False)
    review = report.partial_card_reviews[0].review
    kwargs['novelty_reviews'] = [review]
    report = assemble_report_from_draft(ReportNarrativeDraft(conclusions=[{
        'novelty_point_id': review.novelty_point_id, 'summary': 'A grounded comparison.'}]),
        paper_id=report.paper_id, novelty_points=kwargs['novelty_points'],
        novelty_reviews=[review], evidence_cards=kwargs['evidence_cards'])
    assert not report.conclusions[0].supporting_card_ids
    valid = validate_report_integrity(report, **kwargs)
    assert valid.validation_passed and valid.referenced_card_count == 1
    # Both objects agree: equality alone must not authorize a foreign Card.
    foreign = kwargs['evidence_cards'][0].model_copy(update={'novelty_point_id': 'NP-other'})
    kwargs['evidence_cards'] = [foreign]
    result = validate_report_integrity(report, **kwargs)
    assert result.matched_count == 1
    assert not result.validation_passed
    assert any('cross-point reference' in issue for issue in result.issues)


@pytest.mark.parametrize('scope', ['point', 'card'])
def test_gate_b_rejects_ambiguous_input_identities(tmp_path, scope):
    report, kwargs, _ = case(tmp_path, incremental=False)
    key = 'novelty_points' if scope == 'point' else 'evidence_cards'
    kwargs[key] = kwargs[key] * 2
    result = validate_report_integrity(report, **kwargs)
    assert not result.validation_passed
    assert any('duplicate input point' in issue or 'duplicate authorized card' in issue
               for issue in result.issues)
