"""Offline tests for the configured workflow boundary and service ownership."""
import asyncio
from dataclasses import replace
import socket

import pytest

from novelty_agent_framework.agents.evidence_validator import DefaultEvidenceValidator
from novelty_agent_framework.ports import DoiMetadataRecord, MetadataQuery, ProviderMetadataResult
from novelty_agent_framework.schemas.failures import FailureCode
from novelty_agent_framework.tools.metadata_retrieval import MetadataRetrievalService
from novelty_agent_framework.workflows import NoveltyWorkflowConfig
from test_integrity_gates import _card, _evidence, _point, _store, _task
from test_metadata_retrieval import provider
from test_workflow import build_workflow as _build_workflow, make_paper, RecordingTaskResearcher


def build_workflow(*args, **kwargs):
    kwargs.setdefault("metadata_verification_enabled", True)
    return _build_workflow(*args, **kwargs)


@pytest.fixture(autouse=True)
def offline(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: pytest.fail("unexpected DNS"))
    async def inline(function, *args, **kwargs):
        return function(*args, **kwargs)
    monkeypatch.setattr(asyncio, "to_thread", inline)


class CompleteResearcher(RecordingTaskResearcher):
    async def ainvoke(self, request):
        result = await super().ainvoke(request)
        manifest = self.reference_store.load_manifest(request.subject_paper_id)
        works = [work.model_copy(update={"authors": ["Alice"], "publication_year": 2020})
                 for work in manifest.works]
        self.reference_store.persist_manifest(request.subject_paper_id, manifest.model_copy(update={"works": works}))
        return result


class FakeProvider:
    source_id = "crossref"
    def __init__(self, *, found=True):
        self.queries, self.closed, self.found = [], False, found
    def retrieve(self, query):
        self.queries.append(query)
        records = (DoiMetadataRecord(source_id=self.source_id, doi=None, record_kind="bibliographic",
                     title=query.title, authors=("Alice",), year=2020),) if self.found else ()
        return ProviderMetadataResult(self.source_id, query, "found" if records else "not_found", "test", records)
    def close(self):
        self.closed = True


def test_real_workflow_entry_enables_service_and_closes_on_each_run(monkeypatch):
    from novelty_agent_framework.tools import metadata_retrieval
    providers = []
    def create():
        item = FakeProvider()
        providers.append(item)
        return MetadataRetrievalService([item], owns_providers=True)
    monkeypatch.setattr(metadata_retrieval, "build_metadata_retrieval_service", create)
    workflow, _ = build_workflow(researcher=CompleteResearcher(), max_rounds=1)
    result = workflow.run(make_paper())
    assert result.evidence_cards and providers[0].queries and providers[0].closed
    # A second invocation owns a new, usable service, never the closed first service.
    result2 = workflow.run(make_paper().model_copy(update={"paper_id": "paper-test-second"}))
    assert result2.evidence_cards and providers[1].queries and providers[1].closed


def test_initialization_failure_and_configuration_disabled_are_distinct(tmp_path):
    store, _ = _store(tmp_path)
    context = dict(tasks=[_task()], evidence=[_evidence()], novelty_points=[_point()], paper_id="paper-1", reference_store=store)
    failed = DefaultEvidenceValidator(metadata_unavailable_reason="initialization_failed", metadata_startup_error_type="RuntimeError").validate([_card()], **context)
    disabled = DefaultEvidenceValidator(metadata_unavailable_reason="configuration_disabled").validate([_card()], **context)
    assert failed.accepted and disabled.accepted
    assert failed.integrity["metadata_authenticity"]["card-1"]["reason"] == "initialization_failed"
    assert failed.failure_events[0].code == FailureCode.VALIDATOR_METADATA_INITIALIZATION
    assert disabled.integrity["metadata_authenticity"]["card-1"]["reason"] == "configuration_disabled"
    assert not disabled.failure_events


def test_workflow_initialization_failure_uses_local_mode_and_records_it(monkeypatch):
    from novelty_agent_framework.tools import metadata_retrieval
    def broken():
        raise RuntimeError("secret should never be recorded")
    monkeypatch.setattr(metadata_retrieval, "build_metadata_retrieval_service", broken)
    captured = []
    original = DefaultEvidenceValidator.validate
    def capture(self, *args, **kwargs):
        result = original(self, *args, **kwargs)
        captured.append(result)
        return result
    monkeypatch.setattr(DefaultEvidenceValidator, "validate", capture)
    workflow, _ = build_workflow(max_rounds=1)
    result = workflow.run(make_paper())
    assert result.evidence_cards
    assert any(event.code == FailureCode.VALIDATOR_METADATA_INITIALIZATION for event in result.failure_events)
    assert captured[-1].integrity["metadata_unavailable_reason"] == "initialization_failed"
    assert "secret" not in str(captured[-1].failure_events)
    assert all(item["reason"] == "initialization_failed" for item in captured[-1].integrity["metadata_authenticity"].values())


def test_workflow_closes_service_on_graph_failure(monkeypatch):
    from novelty_agent_framework.tools import metadata_retrieval
    item = FakeProvider()
    monkeypatch.setattr(metadata_retrieval, "build_metadata_retrieval_service",
                        lambda: MetadataRetrievalService([item], owns_providers=True))
    workflow, _ = build_workflow(max_rounds=1)
    class BrokenGraph:
        async def ainvoke(self, *args, **kwargs):
            raise RuntimeError("failure")
    workflow.graph = BrokenGraph()
    with pytest.raises(RuntimeError, match="failure"):
        workflow.run(make_paper())
    assert item.closed


def test_workflow_disabled_does_not_construct_service(monkeypatch):
    from novelty_agent_framework.tools import metadata_retrieval
    monkeypatch.setattr(metadata_retrieval, "build_metadata_retrieval_service",
                        lambda: pytest.fail("disabled workflow must not construct a service"))
    captured = []
    original = DefaultEvidenceValidator.validate
    def capture(self, *args, **kwargs):
        result = original(self, *args, **kwargs)
        captured.append(result)
        return result
    monkeypatch.setattr(DefaultEvidenceValidator, "validate", capture)
    workflow, _ = build_workflow(max_rounds=1, metadata_verification_enabled=False)
    result = workflow.run(make_paper())
    assert result.evidence_cards
    assert captured[-1].integrity["metadata_unavailable_reason"] == "configuration_disabled"


def test_workflow_request_failure_rejects_cards_not_entire_run(monkeypatch):
    from novelty_agent_framework.tools import metadata_retrieval
    item = FakeProvider(found=False)
    def fail(query):
        item.queries.append(query)
        return ProviderMetadataResult("crossref", query, "error", "test", error_type="ReadTimeout",
                                      failure_code=FailureCode.PROVIDER_TIMEOUT)
    item.retrieve = fail
    monkeypatch.setattr(metadata_retrieval, "build_metadata_retrieval_service",
                        lambda: MetadataRetrievalService([item], owns_providers=True))
    workflow, _ = build_workflow(researcher=CompleteResearcher(), max_rounds=1)
    result = workflow.run(make_paper())
    assert not result.evidence_cards and item.queries and item.closed
    assert any(event.code == FailureCode.PROVIDER_TIMEOUT for event in result.failure_events)


def test_local_validator_rejects_card_bound_to_different_works(tmp_path):
    store, _ = _store(tmp_path)
    first = _evidence()
    second = first.model_copy(update={"evidence_id": "ev-2", "work_id": "different-work"})
    card = _card().model_copy(update={"evidence_ids": [first.evidence_id, second.evidence_id]})
    result = DefaultEvidenceValidator().validate([card], tasks=[_task()], evidence=[first, second],
        novelty_points=[_point()], paper_id="paper-1", reference_store=store)
    assert not result.accepted
    assert "multiple Works" in result.rejected[0][1]


def test_integrity_audit_persisted_without_runtime_debug(monkeypatch):
    import json
    from novelty_agent_framework.tools import metadata_retrieval
    from novelty_agent_framework.core import RuntimeDebugConfig
    item = FakeProvider()
    monkeypatch.setattr(metadata_retrieval, "build_metadata_retrieval_service",
                        lambda: MetadataRetrievalService([item], owns_providers=True))
    workflow, _ = build_workflow(researcher=CompleteResearcher(), max_rounds=1,
                                 runtime_debug=RuntimeDebugConfig(enabled=False))
    result = workflow.run(make_paper())
    assert result.evidence_cards
    from pathlib import Path
    path = Path("outputs/paper-test/recovery") / workflow.last_runtime_run_id / "round-1-validator.json"
    audit = json.loads(path.read_text())["validator_integrity"]
    assert audit["metadata_mode"] == "external"
    assert all(value["final_result"] == "accepted" for value in audit["metadata_authenticity"].values())


def test_concurrent_runs_reject_shared_workflow_before_service_creation(monkeypatch):
    from novelty_agent_framework.tools import metadata_retrieval
    providers = []
    def create():
        item = FakeProvider()
        providers.append(item)
        return MetadataRetrievalService([item], owns_providers=True)
    monkeypatch.setattr(metadata_retrieval, "build_metadata_retrieval_service", create)
    workflow, _ = build_workflow(max_rounds=1)
    entered = asyncio.Event()
    release = asyncio.Event()
    class Graph:
        async def ainvoke(self, initial, **kwargs):
            entered.set()
            await release.wait()
            raise ValueError("expected end")
    workflow.graph = Graph()
    async def run():
        first = asyncio.create_task(workflow.arun(make_paper()))
        await entered.wait()
        with pytest.raises(RuntimeError, match="已有运行"):
            await workflow.arun(make_paper().model_copy(update={"paper_id": "paper-other"}))
        release.set()
        with pytest.raises(ValueError, match="expected end"):
            await first
    asyncio.run(run())
    assert len(providers) == 1 and providers[0].closed


def test_failed_or_disabled_empty_card_audits_persist_each_round(tmp_path):
    import json
    from novelty_agent_framework.schemas import PaperInput
    workflow, _ = build_workflow(max_rounds=2)
    workflow.validator = DefaultEvidenceValidator(metadata_unavailable_reason="initialization_failed",
        metadata_startup_error_type="ValueError", metadata_startup_error_category="invalid_configuration")
    paper = PaperInput(paper_id="audit-paper", title="Paper", full_text="body")
    from novelty_agent_framework.report_inputs import begin_report_inputs
    (tmp_path / "outputs/audit-paper").mkdir(parents=True)
    begin_report_inputs(tmp_path / "outputs/audit-paper", "audit-paper", run_id="audit-run")
    for round_number in (1, 2):
        state = dict(paper=paper, run_id="audit-run", rounds=round_number, novelty_points=[],
                     all_research_tasks=[], raw_evidence=[], raw_evidence_cards=[])
        asyncio.run(workflow._validate_evidence(state))
    from pathlib import Path
    for round_number in (1, 2):
        path = Path(f"outputs/audit-paper/recovery/audit-run/round-{round_number}-validator.json")
        audit = json.loads(path.read_text())
        assert audit["validator_integrity"]["metadata_startup_error_category"] == "invalid_configuration"
        assert audit["validator_integrity"]["metadata_authenticity"] == {}
        assert audit["failures"][0]["code"] == "validator.metadata.initialization_failed"


def test_startup_failure_is_persisted_even_when_graph_fails_and_debug_off(monkeypatch):
    import json
    from pathlib import Path
    from novelty_agent_framework.core import RuntimeDebugConfig
    from novelty_agent_framework.tools import metadata_retrieval
    def broken():
        raise ValueError("private token")
    monkeypatch.setattr(metadata_retrieval, "build_metadata_retrieval_service", broken)
    workflow, _ = build_workflow(max_rounds=1, runtime_debug=RuntimeDebugConfig(enabled=False))
    class BrokenGraph:
        async def ainvoke(self, *args, **kwargs):
            raise RuntimeError("pre-validation failure")
    workflow.graph = BrokenGraph()
    with pytest.raises(RuntimeError, match="pre-validation failure"):
        workflow.run(make_paper())
    path = Path("outputs/paper-test/recovery") / workflow.last_runtime_run_id / "metadata-service-startup.json"
    audit = json.loads(path.read_text())
    assert audit["mode"] == "local" and audit["unavailable_reason"] == "initialization_failed"
    assert audit["error_category"] == "invalid_configuration" and audit["error_type"] == "ValueError"
    assert "private token" not in path.read_text()


@pytest.mark.parametrize("description,detail", [
    ("timeout must be positive and finite", "timeout_invalid"),
    ("DOI API base_url must be an HTTPS origin without credentials", "base_url_invalid"),
    ("token=private-value", "provider_configuration_invalid"),
])
def test_startup_diagnostics_are_specific_for_known_messages_without_secrets(monkeypatch, description, detail):
    import json
    from pathlib import Path
    from novelty_agent_framework.tools import metadata_retrieval
    from novelty_agent_framework.tools.database_search.providers.common import ProviderConfigurationError
    def broken():
        raise ProviderConfigurationError(description)
    monkeypatch.setattr(metadata_retrieval, "build_metadata_retrieval_service", broken)
    workflow, _ = build_workflow(max_rounds=1)
    result = workflow.run(make_paper())
    assert result.evidence_cards
    path = Path("outputs/paper-test/recovery") / workflow.last_runtime_run_id / "metadata-service-startup.json"
    assert json.loads(path.read_text())["error_detail"] == detail
    assert "private-value" not in path.read_text()
    assert "private-value" not in str(result.failure_events)
