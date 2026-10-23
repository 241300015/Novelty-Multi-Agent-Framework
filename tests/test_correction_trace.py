"""Issue #21: failure catalog and node-local correction audit contract."""

from __future__ import annotations

import asyncio
import json

import pytest
from pydantic import ValidationError

from backend.env import ModelResponse, ModelToolCall
from novelty_agent_framework.core.correction_trace import (
    record_advice,
    record_detection,
    record_execution,
    record_verification,
    record_verification_pending,
)
from novelty_agent_framework.core.runtime_artifacts import (
    RuntimeArtifactManager,
    RuntimeDebugConfig,
)
from novelty_agent_framework.core.tool_call_harness import (
    ToolCallHarness,
    ToolCallHarnessConfig,
    ToolCallHarnessError,
)
from novelty_agent_framework.schemas import (
    NoveltyPoint,
    ResearcherToolObservation,
    ResearchTask,
    StrictModel,
    TaskResearchRequest,
)
from novelty_agent_framework.schemas.failures import (
    FAILURE_CATALOG,
    FAILURE_DESCRIPTIONS,
    ArtifactVersionRef,
    CorrectionBudgetSnapshot,
    CorrectionStatus,
    FailureCode,
    FailureScope,
    RecoveryAction,
    advance_correction,
    make_correction_record,
    make_failure,
)
from novelty_agent_framework.tools import ResearcherToolRegistry
from novelty_agent_framework.services.jobs import RunStage
from novelty_agent_framework.services.workflow_service import NoveltyWorkflowService
from conftest import minimal_search_plan


def _runtime(tmp_path):
    return RuntimeArtifactManager(
        "paper-1",
        run_id="run-1",
        config=RuntimeDebugConfig(
            output_root=tmp_path / "outputs",
            archive_root=tmp_path / "archive",
        ),
    )


def _scope():
    return TaskResearchRequest(
        subject_paper_id="paper-1",
        run_id="run-1",
        novelty_point=NoveltyPoint(
            point_id="NP-1", claim="claim", technical_features=["feature"]
        ),
        research_task=ResearchTask(
            task_id="T-1", novelty_point_id="NP-1",
            task_type="search", language="en",
        ),
        search_plan=minimal_search_plan("T-1", "NP-1"),
    )


class Arguments(StrictModel):
    value: str


class Tool:
    name = "example"
    description = "test tool"
    args_schema = Arguments

    async def ainvoke(self, arguments, *, scope):
        return ResearcherToolObservation(
            tool_name=self.name,
            arguments=arguments.model_dump(mode="json"),
            succeeded=True,
            payload={"value": arguments.value},
        )


class FailingTool(Tool):
    async def ainvoke(self, arguments, *, scope):
        return ResearcherToolObservation(
            tool_name=self.name,
            arguments=arguments.model_dump(mode="json"),
            succeeded=False,
            error="the corrected request still failed",
        )


class Model:
    def __init__(self, *responses):
        self.responses = list(responses)

    async def acomplete(self, messages, *, options=None):
        return self.responses.pop(0)


def test_all_codes_have_complete_responsibility_metadata_and_one_document_catalog():
    assert len(FailureCode) == len(FAILURE_CATALOG) == len(FAILURE_DESCRIPTIONS) == 149
    assert len(RecoveryAction) == 13
    assert len({definition.retry_limit for definition in FAILURE_CATALOG.values()}) > 1
    for code, definition in FAILURE_CATALOG.items():
        assert definition.observed_fact == FAILURE_DESCRIPTIONS[code]
        assert all((definition.trigger, definition.owner_node,
                    definition.owner_function, definition.stop_boundary))
        assert definition.implementation_status in {
            "implemented", "partial", "not_implemented"
        }
        assert definition.verification_status in {
            "automated", "integration_only", "not_verified"
        }
    assert FAILURE_CATALOG[FailureCode.GATE_REVIEW_MISSING].owner_node == "review_evidence"
    assert "NoveltyEvidenceReviewer" in FAILURE_CATALOG[
        FailureCode.GATE_REVIEW_MISSING
    ].owner_function

    document = open("docs/error-and-supplement-codes.md", encoding="utf-8").read()
    assert all(f"`{code.value}`" in document for code in FailureCode)
    assert all(f"`{action.value}`" in document for action in RecoveryAction)


def test_correction_contract_rejects_false_completion_and_supplement_spending():
    failure = make_failure(
        FailureCode.TOOL_ARGUMENT_SCHEMA,
        scope=FailureScope(run_id="run-1", paper_id="paper-1", point_id="NP-1"),
    )
    record = make_correction_record(failure)
    with pytest.raises(ValueError, match="invalid correction transition"):
        advance_correction(record, CorrectionStatus.VERIFIED)
    with pytest.raises(ValidationError, match="supplement round"):
        CorrectionBudgetSnapshot(
            supplement_round_before=1,
            supplement_round_after=2,
        )
    with pytest.raises(ValidationError, match="passed re-verification"):
        record.model_copy(update={
            "status": CorrectionStatus.VERIFIED,
            "sequence": 2,
            "action": RecoveryAction.REPAIR_ARGUMENTS,
            "attempt": 1,
        }).model_dump()
        type(record).model_validate({
            **record.model_dump(mode="python"),
            "status": CorrectionStatus.VERIFIED,
            "sequence": 2,
            "action": RecoveryAction.REPAIR_ARGUMENTS,
            "attempt": 1,
        })


def test_runtime_preserves_full_correction_history_and_query_index(tmp_path):
    manager = _runtime(tmp_path)
    failure = make_failure(
        FailureCode.TOOL_ARGUMENT_SCHEMA,
        scope=FailureScope(point_id="NP-1", task_id="T-1"),
        occurrence_id="one-chain",
    )
    manager.activate()
    record = record_detection(
        failure,
        occurrence_id="one-chain",
        action_input={"wrong": "value"},
        artifact_versions_before=[ArtifactVersionRef(
            namespace="research_reference", artifact_id="a", sha256="a" * 64,
        )],
    )
    record = record_advice(record, RecoveryAction.REPAIR_ARGUMENTS)
    record = record_execution(record, attempt=1, budget={
        "local_limit": 1,
        "local_used": 1,
        "global_limit": 10,
        "global_used": 4,
        "supplement_round_before": 1,
        "supplement_round_after": 1,
    })
    with pytest.raises(ValueError, match="reset global budget"):
        manager.record_correction(advance_correction(
            record,
            CorrectionStatus.VERIFICATION_PENDING,
            result={"accepted": True},
            budget={
                "global_limit": 10,
                "global_used": 3,
                "supplement_round_before": 1,
                "supplement_round_after": 1,
            },
        ))
    record = record_verification_pending(record, result={"accepted": True})
    record = record_verification(
        record,
        passed=True,
        checker="test.validator",
        details={"schema_valid": True},
        artifact_versions_after=[ArtifactVersionRef(
            namespace="research_reference", artifact_id="a", sha256="b" * 64,
            version="corrected-1",
        )],
    )
    manager.deactivate()

    index = json.loads(
        (manager.run_dir / "corrections" / "index.json").read_text(encoding="utf-8")
    )
    assert index["counts"]["verification_passed"] == 1
    assert index["unresolved_correction_ids"] == []
    assert index["records"][0]["artifact_versions_before"][0]["sha256"] == "a" * 64
    assert index["records"][0]["artifact_versions_after"][0]["sha256"] == "b" * 64
    assert [event["status"] for event in index["events"]] == [
        "detected", "advised", "executing", "verification_pending",
        "verification_passed",
    ]
    assert len(list((manager.run_dir / "corrections" / "events").glob("*.json"))) == 5
    manager.finish_run("SUCCESS")
    manifest = json.loads((manager.run_dir / "manifest.json").read_text())
    summary = json.loads((manager.run_dir / "summary.json").read_text())
    assert manifest["correction_index"]["path"] == "corrections/index.json"
    assert summary["corrections"]["event_count"] == 5


def test_registry_argument_repair_is_executed_and_reverified(tmp_path):
    manager = _runtime(tmp_path)
    registry = ResearcherToolRegistry([Tool()])

    async def run():
        manager.activate()
        try:
            rejected = await registry.execute("example", {"wrong": "value"}, scope=_scope())
            accepted = await registry.execute("example", {"value": "fixed"}, scope=_scope())
        finally:
            manager.deactivate()
        return rejected, accepted

    rejected, accepted = asyncio.run(run())
    assert not rejected.succeeded and accepted.succeeded
    assert rejected.payload["correction"]["status"] == "advised"
    index = manager.correction_index()
    assert index["counts"]["verification_passed"] == 1
    assert [event["status"] for event in index["events"]] == [
        "detected", "advised", "executing", "verification_pending",
        "verification_passed",
    ]


def test_registry_argument_repair_failure_is_reverified_and_stopped(tmp_path):
    manager = _runtime(tmp_path)
    registry = ResearcherToolRegistry([FailingTool()])

    async def run():
        manager.activate()
        try:
            rejected = await registry.execute("example", {"wrong": "value"}, scope=_scope())
            failed = await registry.execute("example", {"value": "fixed"}, scope=_scope())
        finally:
            manager.deactivate()
        return rejected, failed

    rejected, failed = asyncio.run(run())
    assert not rejected.succeeded and not failed.succeeded
    index = manager.correction_index()
    assert index["counts"]["stopped"] == 1
    assert index["records"][0]["verification"]["status"] == "failed"
    assert index["records"][0]["remaining_issues"] == [
        "the corrected request still failed"
    ]
    assert [event["status"] for event in index["events"]] == [
        "detected", "advised", "executing", "verification_pending",
        "attempt_failed", "stopped",
    ]


def test_unregistered_tool_is_recorded_as_not_automatically_recoverable(tmp_path):
    manager = _runtime(tmp_path)
    registry = ResearcherToolRegistry()

    manager.activate()
    try:
        observation = asyncio.run(
            registry.execute("missing", {"value": "ignored"}, scope=_scope())
        )
    finally:
        manager.deactivate()

    assert not observation.succeeded
    assert observation.payload["failure"]["code"] == "tool.dispatch.not_registered"
    assert observation.payload["correction"]["status"] == "stopped"
    index = manager.correction_index()
    assert index["counts"]["stopped"] == 1
    assert index["records"][0]["action"] == "manual"
    assert index["records"][0]["attempt"] == 0


def test_harness_budget_stop_is_terminal_and_does_not_create_a_retry(tmp_path):
    manager = _runtime(tmp_path)
    model = Model(
        ModelResponse(content=None, tool_calls=(
            ModelToolCall("one", "example", {"value": "one"}),
        )),
        ModelResponse(content=None, tool_calls=(
            ModelToolCall("two", "example", {"value": "two"}),
        )),
    )
    harness = ToolCallHarness(
        model,
        ResearcherToolRegistry([Tool()]),
        config=ToolCallHarnessConfig(max_turns=3, max_tool_calls=1),
    )

    manager.activate()
    try:
        with pytest.raises(ToolCallHarnessError, match="tool-call budget"):
            asyncio.run(harness.run(
                system_prompt="system", initial_user_message="task", scope=_scope()
            ))
    finally:
        manager.deactivate()
    index = manager.correction_index()
    assert index["counts"]["stopped"] == 1
    assert index["unresolved_correction_ids"] == []
    assert index["records"][0]["failure_code"] == "harness.budget.tool_calls_exhausted"
    assert index["records"][0]["attempt"] == 0


def test_artifact_version_requires_real_hash():
    with pytest.raises(ValidationError):
        ArtifactVersionRef(namespace="research_reference", artifact_id="a", sha256="not-a-hash")


def test_running_service_progress_projects_latest_correction_index(tmp_path):
    service = NoveltyWorkflowService(
        workflow_factory=lambda _: None,
        runs_root=tmp_path / "runs",
    )
    snapshot = service.create_run()
    service.store.mark_running(snapshot.task_id, stage=RunStage.RESEARCH)
    index_path = (
        tmp_path / "runs" / snapshot.task_id / "paper-1" / "runtime" / "run-1"
        / "corrections" / "index.json"
    )
    index_path.parent.mkdir(parents=True)
    index_path.write_text(json.dumps({
        "counts": {"advised": 1},
        "unresolved_correction_ids": ["correction_" + "a" * 24],
        "events": [{"status": "detected"}, {"status": "advised"}],
    }), encoding="utf-8")

    current = service.get_run(snapshot.task_id)
    assert current.progress.corrections.counts == {"advised": 1}
    assert current.progress.corrections.event_count == 2
    assert service.get_corrections(snapshot.task_id)["events"][1]["status"] == "advised"
