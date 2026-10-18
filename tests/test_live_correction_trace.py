"""Issue #21 live acceptance at the persisted-runtime and HTTP boundaries.

This test uses the real runtime recorder, filesystem index, FastAPI application
and router.  It needs no external model, credentials or paid service.
"""

from __future__ import annotations

from fastapi.testclient import TestClient
import pytest

from novelty_agent_framework.config import NoveltyWebSettings
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
from novelty_agent_framework.main import create_app
from novelty_agent_framework.schemas.failures import (
    FailureCode,
    FailureScope,
    RecoveryAction,
    make_failure,
)
from novelty_agent_framework.services.jobs import RunStage
from novelty_agent_framework.services.workflow_service import NoveltyWorkflowService


pytestmark = pytest.mark.live


def test_live_correction_chain_is_queryable_through_fastapi(tmp_path) -> None:
    runs_root = tmp_path / "runs"
    service = NoveltyWorkflowService(
        workflow_factory=lambda _: None,
        runs_root=runs_root,
    )
    snapshot = service.create_run()
    service.store.mark_running(snapshot.task_id, stage=RunStage.RESEARCH)

    runtime = RuntimeArtifactManager(
        "paper-live",
        run_id="run-live",
        config=RuntimeDebugConfig(
            output_root=runs_root / snapshot.task_id,
            archive_root=tmp_path / "archive",
        ),
    )
    runtime.activate()
    try:
        failure = make_failure(
            FailureCode.TOOL_ARGUMENT_SCHEMA,
            scope=FailureScope(
                run_id="run-live",
                paper_id="paper-live",
                point_id="NP-LIVE",
                task_id="TASK-LIVE",
                node="researcher_tool_registry.arguments",
            ),
            occurrence_id="live-http-chain",
        )
        correction = record_detection(
            failure,
            occurrence_id="live-http-chain",
            action_input={"query": 7},
            budget={
                "local_limit": 1,
                "local_used": 0,
                "global_limit": 8,
                "global_used": 2,
                "supplement_round_before": 1,
                "supplement_round_after": 1,
            },
        )
        correction = record_advice(
            correction,
            RecoveryAction.REPAIR_ARGUMENTS,
            action_input={"query": "graph learning"},
        )
        correction = record_execution(
            correction,
            attempt=1,
            budget={
                "local_limit": 1,
                "local_used": 1,
                "global_limit": 8,
                "global_used": 3,
                "supplement_round_before": 1,
                "supplement_round_after": 1,
            },
        )
        correction = record_verification_pending(
            correction,
            result={"accepted": True},
        )
        record_verification(
            correction,
            passed=True,
            checker="live.fastapi.acceptance",
            details={"schema_valid": True},
        )
    finally:
        runtime.deactivate()
    runtime.finish_run("SUCCESS")

    settings = NoveltyWebSettings(workflow_mode="demo", runs_root=runs_root)
    with TestClient(create_app(settings, service=service)) as client:
        progress = client.get(f"/api/novelty/runs/{snapshot.task_id}")
        corrections = client.get(
            f"/api/novelty/runs/{snapshot.task_id}/corrections"
        )

    assert progress.status_code == 200
    assert progress.headers["cache-control"] == "no-store"
    projected = progress.json()["progress"]["corrections"]
    assert projected["counts"]["verification_passed"] == 1
    assert projected["unresolved_correction_ids"] == []
    assert projected["event_count"] == 5

    assert corrections.status_code == 200
    assert corrections.headers["cache-control"] == "no-store"
    index = corrections.json()
    assert index["run_id"] == "run-live"
    assert index["paper_id"] == "paper-live"
    assert index["records"][0]["scope"]["task_id"] == "TASK-LIVE"
    assert index["records"][0]["action_input"] == {"query": "graph learning"}
    assert [event["status"] for event in index["events"]] == [
        "detected",
        "advised",
        "executing",
        "verification_pending",
        "verification_passed",
    ]
