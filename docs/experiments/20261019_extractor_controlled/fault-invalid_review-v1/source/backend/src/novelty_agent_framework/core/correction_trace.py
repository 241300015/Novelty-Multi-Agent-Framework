"""Persistence helpers for node-local correction facts.

The helpers never choose a recovery action and never execute one.  Owning
Reader/Research/Builder/Validator/Reviewer/Harness code records what it really
did; RuntimeArtifactManager preserves and indexes the immutable snapshots.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from ..schemas.failures import (
    ArtifactVersionRef,
    CorrectionBudgetSnapshot,
    CorrectionRecord,
    CorrectionStatus,
    CorrectionTargets,
    CorrectionVerification,
    CorrectionVerificationStatus,
    FailureEvent,
    RecoveryAction,
    advance_correction,
    make_correction_record,
)
from .runtime_artifacts import current_runtime_artifacts


def record_detection(
    failure: FailureEvent,
    *,
    owner_node: str | None = None,
    owner_function: str | None = None,
    occurrence_id: str | None = None,
    action_input: dict[str, Any] | None = None,
    targets: CorrectionTargets | dict | None = None,
    budget: CorrectionBudgetSnapshot | dict | None = None,
    artifact_versions_before: list[ArtifactVersionRef] | None = None,
) -> CorrectionRecord:
    record = make_correction_record(
        failure,
        owner_node=owner_node,
        owner_function=owner_function,
        occurrence_id=occurrence_id or uuid.uuid4().hex,
        action_input=action_input,
        targets=targets,
        budget=budget,
        artifact_versions_before=artifact_versions_before,
    )
    return persist_correction(record)


def record_advice(
    record: CorrectionRecord,
    action: RecoveryAction,
    *,
    action_input: dict[str, Any] | None = None,
    remaining_issues: list[str] | None = None,
) -> CorrectionRecord:
    return persist_correction(advance_correction(
        record,
        CorrectionStatus.ADVISED,
        action=action,
        action_input=action_input if action_input is not None else record.action_input,
        remaining_issues=remaining_issues or [],
    ))


def record_execution(
    record: CorrectionRecord,
    *,
    attempt: int,
    budget: CorrectionBudgetSnapshot | dict | None = None,
) -> CorrectionRecord:
    return persist_correction(advance_correction(
        record,
        CorrectionStatus.EXECUTING,
        attempt=attempt,
        budget=CorrectionBudgetSnapshot.model_validate(budget or record.budget),
    ))


def record_verification_pending(
    record: CorrectionRecord,
    *,
    result: dict[str, Any],
) -> CorrectionRecord:
    return persist_correction(advance_correction(
        record,
        CorrectionStatus.VERIFICATION_PENDING,
        result=result,
        verification=CorrectionVerification(
            status=CorrectionVerificationStatus.PENDING,
        ),
    ))


def record_verification(
    record: CorrectionRecord,
    *,
    passed: bool,
    checker: str,
    details: dict[str, Any] | None = None,
    remaining_issues: list[str] | None = None,
    result: dict[str, Any] | None = None,
    artifact_versions_after: list[ArtifactVersionRef] | None = None,
) -> CorrectionRecord:
    return persist_correction(advance_correction(
        record,
        CorrectionStatus.VERIFIED if passed else CorrectionStatus.ATTEMPT_FAILED,
        result=result if result is not None else record.result,
        verification=CorrectionVerification(
            status=(CorrectionVerificationStatus.PASSED if passed
                    else CorrectionVerificationStatus.FAILED),
            checker=checker,
            details=details or {},
            checked_at=datetime.now(timezone.utc),
        ),
        remaining_issues=[] if passed else (remaining_issues or ["correction did not resolve the failure"]),
        artifact_versions_after=(artifact_versions_after
                                 if artifact_versions_after is not None
                                 else record.artifact_versions_after),
        stop_reason=None,
    ))


def record_stop(
    record: CorrectionRecord,
    *,
    reason: str,
    action: RecoveryAction | None = None,
    remaining_issues: list[str] | None = None,
    result: dict[str, Any] | None = None,
) -> CorrectionRecord:
    return persist_correction(advance_correction(
        record,
        CorrectionStatus.STOPPED,
        action=action or record.action or RecoveryAction.STOP,
        result=result if result is not None else record.result,
        remaining_issues=remaining_issues or record.remaining_issues,
        stop_reason=reason,
    ))


def persist_correction(record: CorrectionRecord) -> CorrectionRecord:
    runtime = current_runtime_artifacts()
    return runtime.record_correction(record) if runtime is not None else record


__all__ = [
    "persist_correction",
    "record_advice",
    "record_detection",
    "record_execution",
    "record_stop",
    "record_verification",
    "record_verification_pending",
]
