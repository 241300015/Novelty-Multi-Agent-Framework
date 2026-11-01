"""Executed comparisons and skipped requests carry distinct structured causes."""
import asyncio
import json

import httpx
import pytest

from novelty_agent_framework.ports import MetadataQuery, ProviderMetadataResult
from novelty_agent_framework.schemas.failures import (
    FailureCode as C, FailureEvent, FailureScope, make_failure,
)
from novelty_agent_framework.tools.database_search.providers.crossref import CrossrefMetadataTool
from novelty_agent_framework.tools.database_search.providers.doi_common import DoiResponseError
from novelty_agent_framework.tools.database_search.providers.common import ProviderRequestError
from novelty_agent_framework.core.failure_classification import provider_failure
from novelty_agent_framework.tools.metadata_retrieval import MetadataRetrievalService
from novelty_agent_framework.core.metadata_verification import compare_metadata
from novelty_agent_framework.agents.evidence_validator import DefaultEvidenceValidator
from test_doi_metadata_providers import tool_with_handler
from test_metadata_retrieval import provider, record
from test_metadata_authenticity import validate, doi_card
from test_integrity_gates import _store, _card, _evidence, _task, _point


@pytest.mark.parametrize('exc,expected', [
    (httpx.ReadTimeout('private URL must not be serialized'), C.PROVIDER_TIMEOUT),
    (httpx.ConnectError('private URL must not be serialized'), C.PROVIDER_CONNECTION),
    (DoiResponseError('private data'), C.PROVIDER_RESULT_INVALID),
    (ProviderRequestError('secret', status_code=429), C.PROVIDER_RATE_LIMIT),
    (ProviderRequestError('secret', status_code=503), C.PROVIDER_SERVICE),
])
def test_provider_result_preserves_catalogue_code(exc, expected):
    def fail(request):
        raise exc
    result = tool_with_handler(CrossrefMetadataTool, fail).retrieve(MetadataQuery(title='Work'))
    assert result.status == 'error' and result.failure_code == expected
    assert 'private' not in str(result) and 'secret' not in str(result)


@pytest.mark.parametrize('error_type,code', [('ReadTimeout', C.PROVIDER_TIMEOUT), ('ConnectError', C.PROVIDER_CONNECTION)])
def test_wrapped_transport_keeps_specific_cause(error_type, code):
    error = ProviderRequestError('failure', transport_error_type=error_type)
    event = provider_failure(error, scope=FailureScope(provider='crossref'), occurrence_id='sample')
    assert event.code == code


@pytest.mark.parametrize('records,updates,expected', [
    ([], {}, C.VALIDATOR_METADATA_NO_MATCH),
    ([record(title='Work 1')], {}, C.VALIDATOR_METADATA_FIELDS_MISSING),
    ([record(record_kind='handle')], {}, C.VALIDATOR_METADATA_REGISTRATION_ONLY),
    ([record(title='Work 1', year=2025, authors=('Alice',))],
     {'publication_year': 2026, 'authors': ['Alice']}, C.VALIDATOR_METADATA_IDENTITY_MISMATCH),
])
def test_failed_comparison_is_executed_and_has_exact_code(tmp_path, records, updates, expected):
    result = validate(tmp_path, providers=[provider('crossref', records)], work_updates=updates)
    assert not result.accepted
    assert expected in {event.code for event in result.failure_events}
    for event in result.failure_events:
        assert event.scope.node == 'validate_evidence' and event.scope.card_id == 'card-1'
        assert event.scope.task_id == 'T-1' and event.scope.point_id == 'NP-1'
        assert 'ev-1' in event.evidence_refs and 'research_reference:work:work-1' in event.evidence_refs
        assert FailureEvent.model_validate_json(event.model_dump_json()) == event
    source = result.integrity['metadata_authenticity']['card-1']['sources'][0]
    assert source['query_status'] == 'complete' and source['status'] == 'failed'
    assert expected.value in result.rejected[0][1]
    assert any(issue[0] == expected.value for issue in result.issues)
    assert 'unverified' not in json.dumps(result.integrity)


def test_all_unsupported_is_explicit_and_not_no_match(tmp_path):
    result = validate(tmp_path, providers=[provider('doi_org', status='unsupported')])
    assert [event.code for event in result.failure_events] == [C.VALIDATOR_METADATA_UNSUPPORTED]
    assert result.integrity['metadata_authenticity']['card-1']['sources'][0]['query_status'] == 'unsupported'


def test_every_candidate_failure_is_retained():
    result = MetadataRetrievalService([provider('crossref', [
        record(doi='10.1234/a', title='Work', year=2025, authors=('Alice',)),
        record(doi='10.1234/b', title='Work', authors=('Alice',)),
    ])]).retrieve(MetadataQuery(title='Work', year=2026, authors=('Alice',)))
    status, matched, findings = compare_metadata(result.query, result)
    assert status == 'failed' and matched is None
    assert [item.code for item in findings] == [C.VALIDATOR_METADATA_IDENTITY_MISMATCH, C.VALIDATOR_METADATA_FIELDS_MISSING]
    assert findings[0].fields == ('year',)
    assert findings[0].candidate_id != findings[1].candidate_id


def test_partial_provider_error_is_recorded_even_when_identity_passes(tmp_path):
    broken = provider('datacite')
    broken.retrieve = lambda query: ProviderMetadataResult('datacite', query, 'error', 'datacite_findable',
        http_status=429, error_type='ProviderRequestError', failure_code=C.PROVIDER_RATE_LIMIT)
    result = validate(tmp_path, card=doi_card(), providers=[provider('crossref', [record(title='Work 1')]), broken])
    assert result.accepted
    assert [event.code for event in result.failure_events] == [C.PROVIDER_RATE_LIMIT]
    assert result.failure_events[0].scope.provider == 'datacite'
    assert 'HTTP=429' in result.failure_events[0].message


def test_error_events_reach_workflow_state_and_persisted_ledger(tmp_path, monkeypatch):
    from novelty_agent_framework.schemas import PaperInput
    from novelty_agent_framework.workflows import NoveltyWorkflow
    monkeypatch.chdir(tmp_path)
    store, _ = _store(tmp_path / 'outputs')
    workflow = NoveltyWorkflow.default()
    workflow.reference_store = store
    workflow.validator = DefaultEvidenceValidator(metadata_retrieval=MetadataRetrievalService([provider('crossref')]))
    from novelty_agent_framework.report_inputs import begin_report_inputs
    begin_report_inputs(tmp_path / 'outputs/paper-1', 'paper-1', run_id='test-run')
    earlier = make_failure(C.PROVIDER_RATE_LIMIT, scope={'paper_id': 'paper-1'}, occurrence_id='previous')
    state = dict(paper=PaperInput(paper_id='paper-1', title='Paper', full_text='body'),
        run_id='test-run', rounds=1, novelty_points=[_point()], all_research_tasks=[_task()],
        raw_evidence=[_evidence()], raw_evidence_cards=[_card()], failure_events=[earlier])
    result = asyncio.run(workflow._validate_evidence(state))
    assert not result['evidence_cards']
    assert result['failure_events'][0].code == C.PROVIDER_RATE_LIMIT
    misses = [event for event in result['failure_events'] if event.code == C.VALIDATOR_METADATA_NO_MATCH]
    assert {event.scope.provider for event in misses} == {None, 'crossref'}
    audit = json.loads((tmp_path / 'outputs/paper-1/recovery/test-run/round-1-validator.json').read_text())
    assert audit['failures'] == [event.model_dump(mode='json') for event in result['failure_events']]
    assert audit['decisions'] == [] and audit['semantic_verdict_created'] is False
    rerun = asyncio.run(workflow._validate_evidence({**state, 'failure_events': result['failure_events']}))
    assert len(rerun['failure_events']) == len(result['failure_events'])


def test_metadata_error_enters_runtime_correction_index(tmp_path):
    from test_correction_trace import _runtime
    manager = _runtime(tmp_path)
    manager.activate()
    try:
        result = validate(tmp_path / 'fixtures', providers=[provider('crossref')])
    finally:
        manager.deactivate()
    assert result.failure_events[0].scope.run_id == manager.run_id
    index = json.loads((manager.run_dir / 'corrections/index.json').read_text())
    assert index['records'][0]['failure_code'] == C.VALIDATOR_METADATA_NO_MATCH.value
    assert index['records'][0]['status'] == 'detected'
    assert index['records'][0]['targets']['card_ids'] == ['card-1']
    assert index['records'][0]['targets']['evidence_ids'] == ['ev-1']
