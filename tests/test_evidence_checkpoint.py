"""Fault-injection checks for opt-in, invocation-isolated evidence checkpoints."""
from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import replace

import pytest

from backend.env import ModelResponse, ModelToolCall
from backend.env.model_client import ModelContextAdmissionError
from novelty_agent_framework.agents import DefaultEvidenceValidator
from novelty_agent_framework.tools import EvidenceCardBuilder, ReaderTool, ReferenceArtifactReaderTool, ResearcherToolRegistry
from novelty_agent_framework.tools.evidence_checkpoint import EvidenceCheckpointSession, EvidenceCheckpointArguments
from novelty_agent_framework.workflows import TaskResearcherConfig, TaskResearcherWorkflow
from test_evidence_card_builder import prepare_store, scope, card, quote, TEXT_A, TEXT_B


class FaultModel:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    async def acomplete(self, messages, *, options=None):
        self.calls.append((messages, options))
        result = self.responses.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


def tool(name, args):
    return ModelResponse(content=None, tool_calls=(ModelToolCall(f"call-{name}", name, args),))


def reader():
    return tool("reader", {"artifact_id": "art_a", "max_chars": 1000})


def submission(*drafts):
    drafts = drafts or (card(quote("Alpha unique quote.")),)
    return tool("submit_evidence", {"cards": [item.model_dump(mode="json") for item in drafts]})


def finish(*drafts):
    payload = {"cards": [item.model_dump(mode="json") for item in drafts]}
    if not drafts:
        payload["no_evidence_reason"] = "no further evidence"
    return ModelResponse(content=json.dumps(payload))


@pytest.fixture
def builder(tmp_path):
    # Reuse existing Builder source fixture, with real files/hashes for replay.
    store = prepare_store(tmp_path)
    manifest = store.load_manifest("paper-1")
    artifacts = []
    for artifact, content in zip(manifest.artifacts, (TEXT_A, TEXT_B), strict=True):
        store.write_document("paper-1", work_id=artifact.work_id, artifact_id=artifact.artifact_id,
                             extension="txt", content=content)
        artifacts.append(artifact.model_copy(update={"sha256": hashlib.sha256(content.encode()).hexdigest()}))
    store.persist_manifest("paper-1", manifest.model_copy(update={"artifacts": artifacts}))
    return EvidenceCardBuilder(store)


def workflow(builder, model, **options):
    registry = ResearcherToolRegistry([ReaderTool(ReferenceArtifactReaderTool(builder.reference_store))])
    return TaskResearcherWorkflow(model, registry, builder,
        config=TaskResearcherConfig(enable_evidence_checkpoint=True, max_steps=8,
                                   max_tool_calls=6, **options))


def checkpoint_path(builder):
    paths = list(builder.reference_store.output_root.glob("paper-1/evidence-checkpoints/*/*.json"))
    assert len(paths) == 1
    return paths[0]


@pytest.mark.parametrize("failure", [RuntimeError("transport offline"), ModelContextAdmissionError({"code": "context_window_exceeded", "reason": "context admission rejected"})])
def test_checkpoint_survives_later_model_or_context_failure(builder, failure):
    model = FaultModel(reader(), submission(), failure)
    runner = workflow(builder, model)
    result = asyncio.run(runner.ainvoke(scope()))
    assert result.status.value == "partial"
    assert len(result.evidence_cards) == len(result.evidence) == 1
    assert any(str(failure) in warning for warning in result.warnings)
    assert result.evidence[0].quote == "Alpha unique quote."
    assert DefaultEvidenceValidator().validate(result.evidence_cards, tasks=[scope().research_task], evidence=result.evidence, novelty_points=[scope().novelty_point], paper_id="paper-1", reference_store=builder.reference_store).accepted
    from novelty_agent_framework.core.integrity_gates import validate_synthesis_input
    assert validate_synthesis_input(result.evidence_cards, evidence=result.evidence,
        tasks=[scope().research_task], novelty_points=[scope().novelty_point],
        paper_id="paper-1", reference_store=builder.reference_store).accepted
    saved = json.loads(checkpoint_path(builder).read_text())
    assert saved["validation_stage"] == "builder_provenance_only"
    assert saved["execution_status"] == "partial"
    assert any(str(failure) in warning for warning in saved["failure_warnings"])
    assert "submit_evidence" not in runner.tools.names  # no shared-registry mutation
    assert any(item.name == "submit_evidence" for item in model.calls[0][1].tools)


def test_budget_finalization_failure_keeps_checkpoint_and_cause(builder):
    runner = workflow(builder, FaultModel(reader(), submission(), RuntimeError("finalization offline")))
    runner.harness.config = replace(runner.harness.config, max_tool_calls=2, max_turns=2)
    result = asyncio.run(runner.ainvoke(scope()))
    assert result.status.value == "partial"
    assert len(result.evidence_cards) == 1
    assert any("budget finalization failed" in warning and "finalization offline" in warning for warning in result.warnings)


def test_invalid_final_json_and_failed_repair_keep_checkpoint(builder):
    runner = workflow(builder, FaultModel(reader(), submission(), ModelResponse(content="broken"), RuntimeError("repair failed")))
    result = asyncio.run(runner.ainvoke(scope()))
    assert result.status.value == "partial"
    assert len(result.evidence_cards) == 1
    assert any("invalid ResearchFinishDraft" in warning for warning in result.warnings)


@pytest.mark.parametrize("text,read_first", [("fabricated source quote", True), ("Alpha unique quote.", False), ("Beta unique quote.", True)])
def test_ungrounded_or_unread_drafts_never_become_checkpoint_evidence(builder, text, read_first):
    responses = ([reader()] if read_first else []) + [submission(card(quote(text))), RuntimeError("later failure")]
    result = asyncio.run(workflow(builder, FaultModel(*responses)).ainvoke(scope()))
    assert result.status.value == "partial"
    assert result.evidence == result.evidence_cards == []
    saved = json.loads(checkpoint_path(builder).read_text())
    assert saved["accepted_drafts"] == []
    assert saved["builder_result"]["evidence_cards"] == []


def test_normal_finish_and_checkpoint_merge_without_duplicate_cards(builder):
    draft = card(quote("Alpha unique quote."))
    result = asyncio.run(workflow(builder, FaultModel(reader(), submission(draft), finish(draft))).ainvoke(scope()))
    assert result.status.value == "completed"
    assert len(result.evidence_cards) == len(result.evidence) == 1


def test_finish_can_revise_valid_card_but_omitting_it_does_not_erase_checkpoint(builder):
    revised = card(quote("Another A quote.")).model_copy(update={"main_contribution": "revised supported semantics"})
    result = asyncio.run(workflow(builder, FaultModel(reader(), submission(), finish(revised))).ainvoke(scope()))
    assert len(result.evidence_cards) == len(result.evidence) == 1
    assert result.evidence_cards[0].main_contribution == "revised supported semantics"
    assert result.evidence[0].quote == "Another A quote."


@pytest.mark.parametrize("tamper", [False, True])
def test_checkpoint_scores_do_not_replace_validator_provenance_checks(builder, tamper):
    low = card(quote("Alpha unique quote.")).model_copy(update={"confidence": 0.1, "relevance": 0.1})
    result = asyncio.run(workflow(builder, FaultModel(reader(), submission(low), RuntimeError("interrupted"))).ainvoke(scope()))
    assert len(result.evidence_cards) == 1
    if tamper:
        artifact = builder.reference_store.find_artifact("paper-1", "art_a")
        path = builder.reference_store.output_root / "paper-1/references" / artifact.relative_path
        path.write_text("tampered content")
    validated = DefaultEvidenceValidator().validate(result.evidence_cards, tasks=[scope().research_task], evidence=result.evidence, novelty_points=[scope().novelty_point], paper_id="paper-1", reference_store=builder.reference_store)
    assert bool(validated.accepted) is (not tamper)
    assert bool(validated.rejected) is tamper


def test_explicit_recovery_rechecks_artifact_and_preserves_failure(builder):
    runner = workflow(builder, FaultModel(reader(), submission(), RuntimeError("transport offline")))
    original = asyncio.run(runner.ainvoke(scope()))
    invocation = checkpoint_path(builder).stem
    recovered = runner.recover_checkpoint(scope(), invocation_id=invocation)
    assert recovered.status.value == "partial"
    assert recovered.evidence_cards == original.evidence_cards
    assert recovered.evidence == original.evidence
    assert any("transport offline" in warning for warning in recovered.warnings)
    assert len(runner.model_client.calls) == 3
    artifact = builder.reference_store.find_artifact("paper-1", "art_a")
    path = builder.reference_store.output_root / "paper-1/references" / artifact.relative_path
    path.write_text("tampered content")
    with pytest.raises(ValueError, match="sha256 mismatch"):
        runner.recover_checkpoint(scope(), invocation_id=invocation)


def test_cancellation_after_acknowledged_submit_remains_explicitly_recoverable(builder):
    runner = workflow(builder, FaultModel(reader(), submission(), asyncio.CancelledError()))
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(runner.ainvoke(scope()))
    recovered = runner.recover_checkpoint(scope(), invocation_id=checkpoint_path(builder).stem)
    assert recovered.status.value == "partial"
    assert len(recovered.evidence_cards) == 1
    assert any("original execution in_progress" in warning for warning in recovered.warnings)


def test_repeated_invocations_do_not_reuse_reads_or_cards(builder):
    model = FaultModel(reader(), submission(), RuntimeError("first failure"), submission(), RuntimeError("second failure"))
    runner = workflow(builder, model)
    first = asyncio.run(runner.ainvoke(scope()))
    second = asyncio.run(runner.ainvoke(scope()))
    assert len(first.evidence_cards) == 1
    assert second.evidence_cards == []
    assert len(list(builder.reference_store.output_root.glob("paper-1/evidence-checkpoints/*/*.json"))) == 2


@pytest.mark.parametrize("change", ["run", "task", "point", "attempt", "plan"])
def test_cross_scope_recovery_and_submission_rejected(builder, change):
    request = scope()
    session = EvidenceCheckpointSession(builder, request)
    other = request.model_copy(deep=True)
    if change == "run": other.run_id = "another-run"
    if change == "task":
        other.research_task.task_id = "another-task"
        other.search_plan.task_id = "another-task"
    if change == "point":
        other.novelty_point.point_id = "another-point"
        other.research_task.novelty_point_id = "another-point"
        other.search_plan.novelty_point_id = "another-point"
    if change == "attempt": other.research_task.attempt = 2
    if change == "plan": other.search_plan.concepts[0].terms = ["different-plan"]
    with pytest.raises(ValueError, match="scope differs"):
        session.submit(EvidenceCheckpointArguments(cards=[card(quote("Alpha unique quote."))]), scope=other)
    # Even manually putting the file at the other scope's hashed address cannot
    # cross the envelope's exact request-binding check.
    other_session = EvidenceCheckpointSession(builder, other, invocation_id=session.invocation_id)
    other_session.path.parent.mkdir(parents=True, exist_ok=True)
    other_session.path.write_text(json.dumps({"schema_version": 1, "invocation_id": session.invocation_id,
                                               "scope": request.model_dump(mode="json")}))
    with pytest.raises(ValueError, match="scope does not match"):
        EvidenceCheckpointSession.recover(builder, other, invocation_id=session.invocation_id)


@pytest.mark.parametrize("source_limit", ["web", "summary"])
def test_original_source_restrictions_apply_to_checkpoint(builder, source_limit):
    from novelty_agent_framework.schemas import SourceKind

    store = builder.reference_store
    manifest = store.load_manifest("paper-1")
    if source_limit == "web":
        manifest.source_records[0].source_kind = SourceKind.WEB_SUPPLEMENT
    else:
        manifest.artifacts[0].provenance["content_origin"] = "llm_summary"
    store.persist_manifest("paper-1", manifest)
    result = asyncio.run(workflow(builder, FaultModel(reader(), submission(), RuntimeError("later failure"))).ainvoke(scope()))
    assert result.evidence_cards == result.evidence == []


def test_finish_omission_preserves_checkpoint(builder):
    result = asyncio.run(workflow(builder, FaultModel(reader(), submission(), finish())).ainvoke(scope()))
    assert result.status.value == "completed"
    assert len(result.evidence_cards) == 1


def test_submit_evidence_obeys_existing_per_tool_budget(builder):
    second = card(quote("Another A quote."))
    runner = workflow(builder, FaultModel(reader(), submission(), submission(second), finish()))
    runner.harness.config = replace(runner.harness.config, per_tool_limits={"reader": 2, "submit_evidence": 1})
    result = asyncio.run(runner.ainvoke(scope()))
    assert result.status.value == "partial"
    assert result.evidence[0].quote == "Alpha unique quote."
    assert json.loads(checkpoint_path(builder).read_text())["submissions"] == 1


def test_recovery_reapplies_builder_source_restrictions(builder):
    from novelty_agent_framework.schemas import SourceKind

    runner = workflow(builder, FaultModel(reader(), submission(), RuntimeError("offline")))
    asyncio.run(runner.ainvoke(scope()))
    invocation = checkpoint_path(builder).stem
    manifest = builder.reference_store.load_manifest("paper-1")
    manifest.source_records[0].source_kind = SourceKind.WEB_SUPPLEMENT
    builder.reference_store.persist_manifest("paper-1", manifest)
    with pytest.raises(ValueError, match="original EvidenceCardBuilder"):
        runner.recover_checkpoint(scope(), invocation_id=invocation)


def test_parallel_tasks_share_tools_without_sharing_checkpoint_state(builder):
    import re

    class ScopedModel:
        def __init__(self): self.counts = {}

        async def acomplete(self, messages, *, options=None):
            task = re.search(r'"task_id": "([^"\n]+)"', messages[1].content).group(1)
            turn = self.counts.get(task, 0)
            self.counts[task] = turn + 1
            await asyncio.sleep(0)
            if turn == 0: return reader()
            if turn == 1: return submission()
            raise RuntimeError(f"failure for {task}")

    first, second = scope(), scope().model_copy(deep=True)
    second.research_task.task_id = "TASK-2"
    second.search_plan.task_id = "TASK-2"
    runner = workflow(builder, ScopedModel())
    async def invoke():
        return await asyncio.gather(runner.ainvoke(first), runner.ainvoke(second))
    results = asyncio.run(invoke())
    assert all(result.status.value == "partial" for result in results)
    assert [result.evidence[0].task_id for result in results] == ["TASK-1", "TASK-2"]
    assert results[0].evidence_cards[0].card_id != results[1].evidence_cards[0].card_id
    assert len(list(builder.reference_store.output_root.glob("paper-1/evidence-checkpoints/*/*.json"))) == 2


def test_late_finish_builder_exception_preserves_earlier_checkpoint(builder, monkeypatch):
    build = builder.build
    calls = 0
    def fail_after_checkpoint(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls > 1:
            raise ValueError("finish Builder failed")
        return build(*args, **kwargs)
    monkeypatch.setattr(builder, "build", fail_after_checkpoint)
    result = asyncio.run(workflow(builder, FaultModel(reader(), submission(), finish())).ainvoke(scope()))
    assert result.status.value == "partial"
    assert len(result.evidence_cards) == 1
    assert any("finish Builder failed" in warning for warning in result.warnings)


def test_recovery_rebuilds_instead_of_trusting_cached_card_and_evidence_ids(builder):
    runner = workflow(builder, FaultModel(reader(), submission(), RuntimeError("offline")))
    original = asyncio.run(runner.ainvoke(scope()))
    path = checkpoint_path(builder)
    saved = json.loads(path.read_text())
    saved["builder_result"]["evidence"][0]["quote"] = "fabricated cached quote"
    saved["builder_result"]["evidence_cards"][0]["card_id"] = "forged-card-id"
    path.write_text(json.dumps(saved))
    recovered = runner.recover_checkpoint(scope(), invocation_id=path.stem)
    assert recovered.evidence == original.evidence
    assert recovered.evidence_cards == original.evidence_cards
    saved["accepted_drafts"][0]["quotes"][0]["quote"] = "unread and fabricated quote"
    path.write_text(json.dumps(saved))
    with pytest.raises(ValueError, match="original EvidenceCardBuilder"):
        runner.recover_checkpoint(scope(), invocation_id=path.stem)


def test_recovery_rechecks_target_paper_exclusion_when_manifest_metadata_changes(builder):
    from novelty_agent_framework.schemas.research import TargetPaperIdentity

    request = scope().model_copy(update={"target_identity": TargetPaperIdentity(title="Target original paper")})
    runner = workflow(builder, FaultModel(reader(), submission(), RuntimeError("offline")))
    original = asyncio.run(runner.ainvoke(request))
    assert len(original.evidence_cards) == 1
    path = checkpoint_path(builder)
    manifest = builder.reference_store.load_manifest("paper-1")
    manifest.works[0].title = "Target original paper"
    builder.reference_store.persist_manifest("paper-1", manifest)
    with pytest.raises(ValueError, match="target paper artifact"):
        runner.recover_checkpoint(request, invocation_id=path.stem)


def test_valid_revisions_keep_prior_builder_outputs_in_durable_history(builder):
    revised = card(quote("Another A quote."))
    result = asyncio.run(workflow(builder, FaultModel(reader(), submission(), submission(revised),
                                                     RuntimeError("later failure"))).ainvoke(scope()))
    assert len(result.evidence_cards) == 1
    assert result.evidence[0].quote == "Another A quote."
    saved = json.loads(checkpoint_path(builder).read_text())
    assert len(saved["submission_history"]) == 2
    assert saved["submission_history"][0]["builder_result"]["evidence"][0]["quote"] == "Alpha unique quote."
    assert saved["submission_history"][1]["builder_result"]["evidence"][0]["quote"] == "Another A quote."


def test_unrelated_empty_eof_read_does_not_block_valid_checkpoint_recovery(builder):
    eof = tool("reader", {"artifact_id": "art_b", "char_start": len(TEXT_B), "max_chars": 100})
    runner = workflow(builder, FaultModel(reader(), eof, submission(), RuntimeError("offline")))
    original = asyncio.run(runner.ainvoke(scope()))
    assert len(original.evidence_cards) == 1
    assert any(read.text == "" and not read.has_more for read in original.read_results)
    recovered = runner.recover_checkpoint(scope(), invocation_id=checkpoint_path(builder).stem)
    assert recovered.evidence_cards == original.evidence_cards
    assert recovered.evidence == original.evidence


def test_failed_disk_write_never_acknowledges_durable_checkpoint(builder, monkeypatch):
    import novelty_agent_framework.tools.evidence_checkpoint as checkpoint_module

    def fail_write(*args, **kwargs):
        raise OSError("injected disk full")
    monkeypatch.setattr(checkpoint_module, "_atomic_write_json", fail_write)
    model = FaultModel(reader(), submission(), RuntimeError("later model failure"))
    result = asyncio.run(workflow(builder, model).ainvoke(scope()))
    observation = json.loads(model.calls[2][0][-1].content)
    assert observation["succeeded"] is False
    assert "disk full" in observation["error"]
    assert observation.get("durable") is not True
    assert result.status.value == "partial"
    assert any("persistence failed" in warning for warning in result.warnings)
    assert list(builder.reference_store.output_root.glob("paper-1/evidence-checkpoints/*/*.json")) == []
