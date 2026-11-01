"""External metadata must corroborate bound Work, never bypass persisted Evidence."""
import asyncio
import json
import socket

import pytest

from novelty_agent_framework.agents.evidence_validator import DefaultEvidenceValidator
from novelty_agent_framework.tools.metadata_retrieval import MetadataRetrievalService
from test_metadata_retrieval import DOI, record, provider
from test_integrity_gates import _store, _card, _evidence, _point, _task


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("no live network in validator tests")
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)


def validate(tmp_path, *, providers, card=None, work_updates=None, mutate=None, limit=32, async_mode=False):
    store, path = _store(tmp_path)
    if work_updates:
        manifest = store.load_manifest("paper-1")
        work = manifest.works[0].model_copy(update=work_updates)
        store.persist_manifest("paper-1", manifest.model_copy(update={"works": [work]}))
    manifest_before = store.load_manifest("paper-1").model_dump(mode="json")
    evidence = _evidence()
    evidence_before = evidence.model_dump(mode="json")
    validator = DefaultEvidenceValidator(metadata_retrieval=MetadataRetrievalService(providers),
                                         metadata_query_limit=limit)
    if mutate:
        mutate(path)
    kwargs = dict(tasks=[_task()], evidence=[evidence], novelty_points=[_point()], paper_id="paper-1",
                  reference_store=store)
    result = (asyncio.run(validator.avalidate([card or _card()], **kwargs)) if async_mode
              else validator.validate([card or _card()], **kwargs))
    assert store.load_manifest("paper-1").model_dump(mode="json") == manifest_before
    assert evidence.model_dump(mode="json") == evidence_before
    json.dumps(result.integrity)
    return result


def doi_card(doi=DOI):
    card = _card()
    return card.model_copy(update={"sources": [card.sources[0].model_copy(update={"doi": doi})]})


def status(result):
    return result.integrity["metadata_authenticity"]["card-1"]["status"]


def test_known_doi_fills_local_gap_only_after_validator_match(tmp_path):
    result = validate(tmp_path, card=doi_card(), providers=[provider("crossref", [record(title="Work 1")])])
    assert len(result.accepted) == 1 and not result.rejected and status(result) == "passed"
    assert result.integrity["metadata_query_count"] == 1
    check = result.integrity["metadata_authenticity"]["card-1"]["sources"][0]
    assert check["work_id"] == "work-1" and check["evidence_ids"] == ["ev-1"]
    assert check["retrieval"]["cards"][0]["field_sources"]["title"] == ("crossref:0",)


def test_without_doi_search_compares_against_bound_work_year(tmp_path):
    calls = []
    result = validate(tmp_path, work_updates={"publication_year": 2026, "authors": ["Alice"]}, async_mode=True,
        providers=[provider("crossref", [record(title="Work 1", year=2026, authors=("Alice",))], callback=calls.append)])
    assert len(result.accepted) == 1 and status(result) == "passed"
    assert calls[0].doi is None and calls[0].title == "Work 1" and calls[0].year == 2026


@pytest.mark.parametrize("external,accepted", [("Lee, Charlotte E.", True), ("Lee, Charlotte F.", False),
                                              ("E. Lee, Charlotte", False)])
def test_author_order_is_equivalent_but_different_names_are_rejected(tmp_path, external, accepted):
    result = validate(tmp_path, work_updates={"publication_year": 2026, "authors": ["Charlotte E. Lee"]},
        providers=[provider("crossref", [record(title="Work 1", year=2026, authors=(external,))])])
    assert bool(result.accepted) is accepted


@pytest.mark.parametrize("records,updates", [
    ([record(title="Work 1")], {}),
    ([record(title="Work 1", year=2025)], {"publication_year": 2026}),
    ([record(record_kind="handle")], {}),
    ([], {}),
    ([record(title="Work 1", year=2026), record(doi="10.1234/other", title="Work 1", year=2026)],
     {"publication_year": 2026}),
    ([record(title="Work 1", year=2026), record(doi="10.1234/other", title="Work 1")],
     {"publication_year": 2026}),
])
def test_ambiguous_missing_or_inconsistent_facts_emit_specific_errors(tmp_path, records, updates):
    result = validate(tmp_path, work_updates=updates, providers=[provider("crossref", records)])
    assert not result.accepted and status(result) == "failed"
    assert "validator.metadata." in result.rejected[0][1]


def test_real_doi_of_another_title_is_conflict(tmp_path):
    result = validate(tmp_path, card=doi_card(), providers=[provider("crossref", [record(title="Different work")])])
    assert not result.accepted and status(result) == "failed"
    assert "validator.metadata.identity_mismatch" in result.rejected[0][1]


def test_partial_sources_merge_before_validator_comparison(tmp_path):
    result = validate(tmp_path, work_updates={"publication_year": 2026, "authors": ["Alice"]}, providers=[
        provider("crossref", [record(title="Work 1", authors=("Alice",))]),
        provider("datacite", [record("datacite", year=2026)]),
    ])
    assert len(result.accepted) == 1 and status(result) == "passed"


def test_external_field_conflict_emits_source_conflict(tmp_path):
    result = validate(tmp_path, card=doi_card(), providers=[
        provider("crossref", [record(title="Work 1")]),
        provider("datacite", [record("datacite", title="Different")]),
    ])
    assert result.accepted and status(result) == "passed"
    assert any(event.code.value == "validator.metadata.source_conflict" for event in result.failure_events)


def test_network_failure_is_not_a_false_doi_verdict(tmp_path):
    result = validate(tmp_path, card=doi_card(), providers=[provider("crossref", status="error")])
    assert not result.accepted and status(result) == "failed"
    assert "provider.unknown" in result.rejected[0][1]


def test_bad_local_file_never_reaches_external_service(tmp_path):
    calls = []
    result = validate(tmp_path, card=doi_card(), mutate=lambda p: p.write_text("changed"),
        providers=[provider("crossref", [record(title="Work 1")], callback=calls.append)])
    assert not result.accepted and status(result) == "skipped" and not calls


def test_file_changed_during_request_is_rechecked(tmp_path):
    def change_file(query):
        paths = list(tmp_path.rglob("artifact-1.txt"))
        assert len(paths) == 1
        paths[0].write_text("changed during request")
    result = validate(tmp_path, card=doi_card(),
        providers=[provider("crossref", [record(title="Work 1")], callback=change_file)])
    assert not result.accepted and status(result) == "passed"
    assert "hash" in result.rejected[0][1].lower() or "sha256" in result.rejected[0][1].lower()


def test_external_result_cannot_overwrite_known_local_doi(tmp_path):
    calls = []
    from novelty_agent_framework.schemas import ExternalIdentifier
    result = validate(tmp_path, card=doi_card(),
        work_updates={"identifiers": [ExternalIdentifier(namespace="doi", value="10.1234/local")]},
        providers=[provider("crossref", [record(title="Work 1")], callback=calls.append)])
    assert not result.accepted and status(result) == "skipped" and not calls


def test_invalid_local_identifier_emits_input_error(tmp_path):
    from novelty_agent_framework.schemas import ExternalIdentifier
    result = validate(tmp_path,
        work_updates={"identifiers": [ExternalIdentifier(namespace="url", value="https://example.test/work-1"),
                                      ExternalIdentifier(namespace="doi", value="malformed")]},
        providers=[provider("crossref", [record(title="Work 1")])])
    assert not result.accepted and status(result) == "failed"
    assert "validator.metadata.local_invalid" in result.rejected[0][1]


def test_batch_reuses_queries_and_limits_distinct_queries(tmp_path):
    store, _ = _store(tmp_path)
    card = doi_card()
    cards = [card, card.model_copy(update={"card_id": "card-2"}), doi_card("10.1234/other").model_copy(
        update={"card_id": "card-3"})]
    calls = []
    validator = DefaultEvidenceValidator(metadata_retrieval=MetadataRetrievalService([
        provider("crossref", [record(title="Work 1")], callback=calls.append)]), metadata_query_limit=1)
    result = validator.validate(cards, tasks=[_task()], evidence=[_evidence()], novelty_points=[_point()],
                                paper_id="paper-1", reference_store=store)
    assert len(calls) == 1 and len(result.accepted) == 2 and len(result.rejected) == 1
    assert "validator.metadata.query_budget" in result.rejected[0][1]


@pytest.mark.parametrize("conflicting_local_year", [False, True])
def test_source_record_year_can_fill_query_but_conflict_is_not_hidden(tmp_path, conflicting_local_year):
    from novelty_agent_framework.schemas import SourceRecord
    from test_integrity_gates import NOW
    store, _ = _store(tmp_path)
    manifest = store.load_manifest("paper-1")
    source = SourceRecord(source_record_id="source-1", work_id="work-1", source_id="fixture",
        source_kind="structured_database", title="Work 1", publication_year=2026, authors=["Alice"],
        access_status="metadata_only", observed_at=NOW)
    works = manifest.works
    if conflicting_local_year:
        works = [works[0].model_copy(update={"publication_year": 2025})]
    store.persist_manifest("paper-1", manifest.model_copy(update={"source_records": [source], "works": works}))
    calls = []
    validator = DefaultEvidenceValidator(metadata_retrieval=MetadataRetrievalService([
        provider("crossref", [record(title="Work 1", year=2026, authors=("Alice",))], callback=calls.append)]))
    result = validator.validate([_card()], tasks=[_task()], evidence=[_evidence()], novelty_points=[_point()],
                                paper_id="paper-1", reference_store=store)
    assert bool(result.accepted) == (not conflicting_local_year)
    if conflicting_local_year:
        assert not calls and status(result) == "failed"
    else:
        assert calls[0].year == 2026


def test_subject_reference_namespace_gets_its_own_external_identity(tmp_path):
    from novelty_agent_framework.persistence import ReferenceStore
    from test_integrity_gates import _subject_store
    _subject_store(tmp_path)
    ev = _evidence(provenance={"artifact_namespace": "subject_reference"})
    validator = DefaultEvidenceValidator(metadata_retrieval=MetadataRetrievalService([
        provider("crossref", [record(title="Work 1")])]))
    result = validator.validate([doi_card()], tasks=[_task()], evidence=[ev], novelty_points=[_point()],
                                paper_id="paper-1", reference_store=ReferenceStore(tmp_path))
    assert result.accepted
    assert result.integrity["metadata_authenticity"]["card-1"]["sources"][0]["artifact_namespace"] == "subject_reference"


def test_workflow_uses_async_validator_and_retains_metadata_audit(tmp_path, monkeypatch):
    from novelty_agent_framework.schemas import PaperInput
    from novelty_agent_framework.workflows import NoveltyWorkflow
    monkeypatch.chdir(tmp_path)
    store, _ = _store(tmp_path / "outputs")
    workflow = NoveltyWorkflow.default()
    workflow.reference_store = store
    workflow.validator = DefaultEvidenceValidator(metadata_retrieval=MetadataRetrievalService([
        provider("crossref", [record(title="Work 1")])]))
    state = dict(paper=PaperInput(paper_id="paper-1", title="Paper", full_text="body"),
                 novelty_points=[_point()], all_research_tasks=[_task()],
                 raw_evidence=[_evidence()], raw_evidence_cards=[doi_card()])
    result = asyncio.run(workflow._validate_evidence(state))
    assert len(result["validator_accepted_cards"]) == 1
    assert result["evidence_integrity"]["metadata_authenticity"]["card-1"]["status"] == "passed"
    persisted = json.loads((tmp_path / "outputs/paper-1/evidence-cards.json").read_text())
    assert persisted["validator_accepted_cards"][0]["card_id"] == "card-1"


def test_local_doi_without_returned_doi_uses_full_identity_and_reports_no_doi_comparison(tmp_path):
    from novelty_agent_framework.schemas import ExternalIdentifier
    result = validate(tmp_path, card=doi_card(), work_updates={"authors": ["Alice"], "publication_year": 2020,
        "identifiers": [ExternalIdentifier(namespace="doi", value=DOI),
                        ExternalIdentifier(namespace="url", value="https://example.test/work-1")]},
        providers=[provider("crossref", [record(doi=None, title="Work 1", authors=("Alice",), year=2020)])])
    assert result.accepted
    assert result.integrity["metadata_authenticity"]["card-1"]["sources"][0]["doi_comparison"] == "not_available"


def test_no_doi_page_difference_rejects_specific_candidate(tmp_path):
    result = validate(tmp_path, work_updates={"authors": ["Alice"], "publication_year": 2020,
                                              "pages": "1-5", "page_count": 5},
        providers=[provider("crossref", [record(doi=None, title="Work 1", authors=("Alice",),
                                                  year=2020, pages="1-6", page_count=6)])])
    assert not result.accepted
    check = result.integrity["metadata_authenticity"]["card-1"]["sources"][0]
    assert {"pages", "page_count"}.issubset(check["findings"][0]["fields"])


def test_partial_miss_and_provider_error_are_both_recorded(tmp_path):
    from novelty_agent_framework.schemas.failures import FailureCode
    result = validate(tmp_path, card=doi_card(), providers=[provider("crossref"), provider("datacite", status="error")])
    assert not result.accepted
    assert {FailureCode.VALIDATOR_METADATA_NO_MATCH, FailureCode.PROVIDER_UNKNOWN}.issubset(
        {event.code for event in result.failure_events})


def test_matched_candidate_wins_despite_different_candidate_and_conflict_is_recorded(tmp_path):
    result = validate(tmp_path, card=doi_card(), work_updates={"publication_year": 2020}, providers=[
        provider("crossref", [record(title="Work 1", year=2020)]),
        provider("datacite", [record("datacite", title="Work 1", year=2021)]),
    ])
    assert result.accepted and status(result) == "passed"
    check = result.integrity["metadata_authenticity"]["card-1"]["sources"][0]
    assert len(check["retrieval"]["cards"]) == 2
    assert any(event.code.value == "validator.metadata.source_conflict" for event in result.failure_events)
    assert any(event.code.value == "validator.metadata.identity_mismatch" for event in result.failure_events)


def test_unrelated_found_source_and_failure_both_recorded(tmp_path):
    from novelty_agent_framework.schemas.failures import FailureCode
    result = validate(tmp_path, card=doi_card(), providers=[
        provider("crossref", [record(title="Other work")]), provider("datacite", status="error")])
    assert not result.accepted
    assert {FailureCode.VALIDATOR_METADATA_NO_MATCH, FailureCode.PROVIDER_UNKNOWN}.issubset(
        {event.code for event in result.failure_events})
    assert any(event.scope.provider == "crossref" and event.code == FailureCode.VALIDATOR_METADATA_NO_MATCH
               for event in result.failure_events)


def test_unrelated_title_and_missing_authors_with_other_source_timeout_is_a_scoped_miss(tmp_path):
    from novelty_agent_framework.schemas.failures import FailureCode
    result = validate(tmp_path, providers=[provider("crossref", [record(doi=None, title="Different")]),
                                            provider("datacite", status="error")])
    assert not result.accepted
    assert any(event.scope.provider == "crossref" and event.code == FailureCode.VALIDATOR_METADATA_NO_MATCH
               for event in result.failure_events)
    assert any(event.scope.provider == "datacite" and event.code == FailureCode.PROVIDER_UNKNOWN
               for event in result.failure_events)


def test_no_doi_checks_available_publication_fields(tmp_path):
    result = validate(tmp_path, work_updates={"authors": ["Alice"], "publication_year": 2020,
        "venue": "Journal", "publisher": "Publisher", "volume": "3", "issue": "2"},
        providers=[provider("crossref", [record(doi=None, title="Work 1", authors=("Alice",), year=2020,
            venue="Journal", publisher="Other Publisher", volume="4", issue="2")])])
    assert not result.accepted
    findings = result.integrity["metadata_authenticity"]["card-1"]["sources"][0]["findings"]
    assert {"publisher", "volume"}.issubset(findings[0]["fields"])


def test_accepted_candidate_failures_are_partial_and_do_not_block_card(tmp_path):
    from novelty_agent_framework.schemas.failures import RecoveryAction
    result = validate(tmp_path, card=doi_card(), work_updates={"publication_year": 2020}, providers=[
        provider("crossref", [record(title="Work 1", year=2020)]),
        provider("datacite", [record("datacite", title="Work 1", year=2021)]),
    ])
    assert result.accepted and result.failure_events
    for event in result.failure_events:
        assert event.execution_status == "partial" and event.conclusion_effect == "limits_coverage"
        assert event.recovery == RecoveryAction.NONE and not event.retry.allowed
        assert type(event).model_validate_json(event.model_dump_json()) == event


def test_rejected_candidate_identity_mismatch_still_blocks_card(tmp_path):
    from novelty_agent_framework.schemas.failures import FailureCode
    result = validate(tmp_path, card=doi_card(), providers=[provider("crossref", [record(title="Wrong")])])
    assert not result.accepted
    event = next(event for event in result.failure_events if event.code == FailureCode.VALIDATOR_METADATA_IDENTITY_MISMATCH)
    assert event.conclusion_effect == "blocks_card" and event.execution_status == "incomplete"


def test_no_doi_source_conflict_description_does_not_assert_doi(tmp_path):
    from novelty_agent_framework.schemas.failures import FailureCode
    result = validate(tmp_path, work_updates={"authors": ["Alice"], "publication_year": 2020, "pages": "1-5"},
        providers=[provider("crossref", [record(doi=None, title="Work 1", authors=("Alice",), year=2020, pages="1-5")]),
                   provider("datacite", [record("datacite", doi=None, title="Work 1", authors=("Alice",), year=2020, pages="1-6")])])
    assert result.accepted
    events = [event for event in result.failure_events if event.code == FailureCode.VALIDATOR_METADATA_SOURCE_CONFLICT]
    assert events and all("同一 DOI" not in event.message for event in events)
