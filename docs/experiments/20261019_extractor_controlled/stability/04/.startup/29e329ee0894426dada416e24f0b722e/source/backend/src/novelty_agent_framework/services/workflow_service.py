"""论文查新任务的 Web 应用服务。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import tempfile
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path
from threading import RLock
from typing import Any

from fastapi import UploadFile
from backend.env import ModelCallBudgetExceeded, reset_model_call_budget, set_model_call_budget

from novelty_agent_framework.config import (
    NoveltyWebSettings,
    build_model_registry,
    build_standard_full_workflow,
    load_application_config,
)
from novelty_agent_framework.config.experiment import (ConfigurationPreflightError, prepare_startup, require_preflight, _startup_source_files)
from novelty_agent_framework.schemas import PaperInput
from novelty_agent_framework.services.jobs import (
    InMemoryRunStore,
    CorrectionProgress,
    RunError,
    RunReport,
    RunSnapshot,
    RunStage,
)
from novelty_agent_framework.workflows import NoveltyWorkflow
from .model_budget import RunModelBudget

logger = logging.getLogger(__name__)
WorkflowFactory = Callable[[Path], NoveltyWorkflow]

_INTERNAL_STAGE_MAP = {
    "extract_points": RunStage.EXTRACT_POINTS,
    "plan": RunStage.PLAN_RESEARCH,
    "dispatch_planning_tasks": RunStage.PLAN_RESEARCH,
    "plan_research_task": RunStage.PLAN_RESEARCH,
    "dispatch_research_tasks": RunStage.RESEARCH,
    "run_research_task": RunStage.RESEARCH,
    "validate_evidence": RunStage.VALIDATE_EVIDENCE,
    "review_evidence": RunStage.VALIDATE_EVIDENCE,
    "validate_synthesis_input": RunStage.VALIDATE_EVIDENCE,
    "check_final_evidence_sufficiency": RunStage.VALIDATE_EVIDENCE,
    "plan_supplement": RunStage.VALIDATE_EVIDENCE,
    "synthesize_report": RunStage.RENDER_REPORT,
    "validate_report_integrity": RunStage.RENDER_REPORT,
    "persist_report": RunStage.RENDER_REPORT,
    "render_report": RunStage.RENDER_REPORT,
}


class WorkflowConfigurationError(RuntimeError):
    """真实工作流无法安全启动。"""


class UploadValidationError(ValueError):
    def __init__(self, code: str, message: str, *, status_code: int = 422) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


class NoveltyWorkflowService:
    def __init__(
        self,
        *,
        workflow_factory: WorkflowFactory,
        runs_root: str | Path,
        processor: Any | None = None,
        store: InMemoryRunStore | None = None,
        max_upload_bytes: int = 30 * 1024 * 1024,
        run_model_budget_rmb: Decimal | None = None,
        run_model_max_attempts: int | None = 80,
    ) -> None:
        self.workflow_factory = workflow_factory
        self.processor = processor
        self.store = store or InMemoryRunStore()
        self._submission_lock = RLock()
        self._submissions: dict[str, tuple[str, str]] = {}
        self.runs_root = Path(runs_root).resolve()
        self.max_upload_bytes = max_upload_bytes
        self.run_model_budget_rmb = run_model_budget_rmb
        self.run_model_max_attempts = run_model_max_attempts
        self.runs_root.mkdir(parents=True, exist_ok=True)
        (self.runs_root / ".uploads").mkdir(exist_ok=True)

    def create_run(self) -> RunSnapshot:
        snapshot = self.store.create()
        self._run_dir(snapshot.task_id).mkdir(parents=True, exist_ok=False)
        return snapshot

    async def create_file_run(
        self, upload: UploadFile, *, submission_id: str | None = None
    ) -> tuple[RunSnapshot, Path, bool]:
        self._validate_upload_metadata(upload)
        temporary_path: Path | None = None
        try:
            descriptor, raw_path = tempfile.mkstemp(
                prefix="paper-", suffix=".upload", dir=self.runs_root / ".uploads"
            )
            os.close(descriptor)
            temporary_path = Path(raw_path)
            total = 0
            digest = hashlib.sha256()
            first_kib = bytearray()
            with temporary_path.open("wb") as destination:
                while chunk := await upload.read(1024 * 1024):
                    total += len(chunk)
                    if total > self.max_upload_bytes:
                        raise UploadValidationError(
                            "file_too_large",
                            "论文文件超过服务端大小限制。",
                            status_code=413,
                        )
                    if len(first_kib) < 1024:
                        first_kib.extend(chunk[: 1024 - len(first_kib)])
                    digest.update(chunk)
                    destination.write(chunk)
            if total == 0:
                raise UploadValidationError("empty_file", "不能上传空文件。")
            if b"%PDF-" not in bytes(first_kib):
                raise UploadValidationError(
                    "invalid_pdf_signature", "文件内容不是有效的 PDF。"
                )

            with self._submission_lock:
                previous = self._submissions.get(submission_id) if submission_id else None
                if previous is not None:
                    prior_digest, task_id = previous
                    if prior_digest != digest.hexdigest():
                        raise UploadValidationError(
                            "submission_conflict", "同一提交编号对应不同的论文文件。", status_code=409
                        )
                    snapshot = self.store.get(task_id)
                    if snapshot is None:
                        raise UploadValidationError(
                            "submission_unavailable", "原任务已失效，无法确认提交状态。", status_code=409
                        )
                    return snapshot, self._run_dir(task_id) / "input" / "paper.pdf", False
                snapshot = self.create_run()
                input_dir = self._run_dir(snapshot.task_id) / "input"
                input_dir.mkdir()
                destination = input_dir / "paper.pdf"
                os.replace(temporary_path, destination)
                temporary_path = None
                if submission_id:
                    self._submissions[submission_id] = (digest.hexdigest(), snapshot.task_id)
            return snapshot, destination, True
        finally:
            await upload.close()
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)

    async def execute(self, task_id: str, paper: PaperInput) -> None:
        token = self._activate_budget(task_id)
        try:
            self.store.mark_running(task_id, stage=RunStage.EXTRACT_POINTS)
            await self._execute_workflow(task_id, paper)
        finally:
            if token is not None:
                reset_model_call_budget(token)

    async def execute_file(self, task_id: str, path: Path) -> None:
        token = self._activate_budget(task_id)
        try:
            await self._execute_file(task_id, path)
        finally:
            if token is not None:
                reset_model_call_budget(token)

    def _activate_budget(self, task_id: str):
        if self.run_model_budget_rmb is None:
            return None
        budget = RunModelBudget(self._run_dir(task_id) / "budget-ledger.json",
                                cap_rmb=self.run_model_budget_rmb,
                                max_attempts=self.run_model_max_attempts)
        return set_model_call_budget(budget)

    async def _execute_file(self, task_id: str, path: Path) -> None:
        self.store.mark_running(task_id, stage=RunStage.PARSE_PAPER)
        if self.processor is None:
            self.store.mark_failed(
                task_id,
                RunError(
                    code="paper_processing_unavailable",
                    message="论文解析服务尚未正确配置。",
                    retryable=False,
                ),
            )
            return
        try:
            # Construct/preflight and freeze the run before PDF processing can call a model.
            workflow = self.workflow_factory(self._run_dir(task_id))
            document = await asyncio.to_thread(
                self.processor.process, path, paper_id=task_id
            )
            paper = self.processor.to_paper_input(document)
        except Exception as exc:  # noqa: BLE001 - public errors must be normalized
            logger.exception("paper parsing failed for task %s", task_id)
            self.store.mark_failed(
                task_id,
                RunError(
                    code=("model_budget_exhausted" if isinstance(exc, ModelCallBudgetExceeded)
                          else "paper_parse_failed"),
                    message=("模型调用预算已用尽，论文解析未完成。" if isinstance(exc, ModelCallBudgetExceeded)
                             else "论文解析失败，请确认 PDF 可正常打开且包含可识别内容。"),
                    retryable=False,
                ),
            )
            return
        await self._execute_workflow(task_id, paper, workflow=workflow)

    async def _execute_workflow(self, task_id: str, paper: PaperInput,
                                *, workflow: NoveltyWorkflow | None = None) -> None:
        run_dir = self._run_dir(task_id)
        (run_dir / "paper-input.json").write_text(paper.model_dump_json(), encoding="utf-8")

        def progress(stage: str, round: int | None) -> None:
            mapped = _INTERNAL_STAGE_MAP.get(stage)
            if mapped is not None:
                self.store.mark_progress(task_id, mapped, round=round)

        try:
            workflow = workflow or self.workflow_factory(run_dir)
            result = await workflow.arun(paper, progress_callback=progress)
            rendered = workflow.last_rendered_report_path
            report_path = Path(rendered).resolve() if rendered else None
            if (
                report_path is None
                or not report_path.is_file()
                or not report_path.is_relative_to(run_dir.resolve())
            ):
                raise RuntimeError("workflow completed without a task-scoped report")
            base_url = f"/api/novelty/runs/{task_id}/report"
            report = RunReport(
                available_formats=["md"],
                preview_url=f"{base_url}?disposition=inline",
                downloads={"md": f"{base_url}?disposition=attachment"},
                corrections_url=f"/api/novelty/runs/{task_id}/corrections",
            )
            self.store.mark_succeeded(
                task_id,
                result.model_dump(mode="json"),
                report=report,
                report_path=report_path,
            )
        except Exception as exc:  # noqa: BLE001 - public errors must be normalized
            logger.exception("novelty workflow failed for task %s", task_id)
            self.store.mark_failed(
                task_id,
                RunError(
                    code=("model_budget_exhausted" if isinstance(exc, ModelCallBudgetExceeded)
                          else "workflow_failed"),
                    message=("模型调用预算已用尽，查新流程未完成。" if isinstance(exc, ModelCallBudgetExceeded)
                             else "查新工作流未能完成，请稍后重新提交或联系服务管理员。"),
                    retryable=not isinstance(exc, ModelCallBudgetExceeded),
                ),
            )

    def get_run(self, task_id: str) -> RunSnapshot | None:
        snapshot = self.store.get(task_id)
        if snapshot is None or snapshot.progress is None:
            return snapshot
        payload = self.get_corrections(task_id)
        if payload is None:
            return snapshot
        try:
            progress = CorrectionProgress(
                counts=payload.get("counts", {}),
                unresolved_correction_ids=payload.get(
                    "unresolved_correction_ids", []
                ),
                event_count=len(payload.get("events", [])),
            )
        except (OSError, ValueError, TypeError):
            return snapshot
        return snapshot.model_copy(update={
            "progress": snapshot.progress.model_copy(update={
                "corrections": progress,
            })
        })

    def get_corrections(self, task_id: str) -> dict[str, Any] | None:
        indexes = list(self._run_dir(task_id).glob(
            "*/runtime/*/corrections/index.json"
        ))
        if not indexes:
            return None
        try:
            path = max(indexes, key=lambda item: item.stat().st_mtime_ns)
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return None
        return payload if isinstance(payload, dict) else None

    def get_report(self, task_id: str) -> tuple[Path, str] | None:
        snapshot = self.store.get(task_id)
        path = self.store.get_report_path(task_id)
        if snapshot is None or snapshot.report is None or path is None or not path.is_file():
            return None
        return path, f"{task_id}-查新报告.md"

    def _run_dir(self, task_id: str) -> Path:
        path = (self.runs_root / task_id).resolve()
        if not path.is_relative_to(self.runs_root):
            raise ValueError("unsafe task id")
        return path

    @staticmethod
    def _validate_upload_metadata(upload: UploadFile) -> None:
        filename = upload.filename or ""
        if Path(filename).suffix.lower() != ".pdf":
            raise UploadValidationError("unsupported_file_type", "论文原文仅支持 PDF。")
        if upload.content_type and upload.content_type not in {
            "application/pdf",
            "application/octet-stream",
        }:
            raise UploadValidationError(
                "mime_mismatch", "文件类型与扩展名不符，请重新选择。"
            )


def build_real_workflow_service(settings: NoveltyWebSettings) -> NoveltyWorkflowService:
    """构建真实 Web 工作流，并在接收任务前验证关键运行条件。"""

    config = load_application_config()
    if config.reviewer is None or not config.reviewer.enabled:
        raise WorkflowConfigurationError("真实工作流必须启用 Reviewer")
    try:
        require_preflight(config, include_processing=True)
    except ConfigurationPreflightError as exc:
        raise WorkflowConfigurationError(str(exc)) from exc
    registry = build_model_registry(config)
    processing = config.project.processing
    # Long-lived services must restart after source edits; archive the same source
    # version used to construct their shared processor, not a later working tree.
    source_at_service_start = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in _startup_source_files()}

    from novelty_agent_framework.processing import DefaultPaperProcessor, MineruSettings

    processor = DefaultPaperProcessor(
        parser=str(processing.get("parser", "mineru")),
        ocr_client=(
            registry.client_for(processing["ocr_model"])
            if processing.get("ocr_model") and processing.get("ocr_fallback_enabled", True)
            else None
        ),
        llm_client=(
            registry.client_for(processing["llm_model"])
            if processing.get("llm_model")
            else None
        ),
        dpi=int(processing.get("dpi", 200)),
        min_chars_per_page=int(processing.get("quality_min_chars_per_page", 200)),
        mineru_settings=MineruSettings(
            python_path=processing.get("mineru_python"),
            env_name=processing.get("mineru_env", "mineru"),
            worker_path=processing.get("mineru_worker", "scripts/mineru_worker.py"),
            backend=processing.get("mineru_backend", "pipeline"),
            method=processing.get("mineru_method", "auto"),
            lang=processing.get("mineru_lang", "ch"),
            effort=processing.get("mineru_effort", "medium"),
            timeout_seconds=int(processing.get("mineru_timeout_seconds", 1800)),
            work_root=processing.get("mineru_work_root", "outputs/.mineru"),
            model_source=processing.get("mineru_model_source"),
        ),
    )

    def workflow_factory(output_root: Path) -> NoveltyWorkflow:
        run_config = config.model_copy(deep=True)
        run_config.project.runtime_debug.output_root = str(output_root)
        run_config.project.runtime_debug.archive_root = str(
            output_root / "runtime-archive"
        )
        current_sources = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in _startup_source_files()}
        if current_sources != source_at_service_start:
            raise WorkflowConfigurationError("Source changed after service construction; restart before starting another run.")
        input_path = next((p for p in (output_root / "input" / "paper.pdf", output_root / "paper-input.json") if p.is_file()), None)
        prepare_startup(run_config, output_root=output_root, entrypoint="workflow_service",
                        snapshot_dir=output_root / "startup", input_path=input_path,
                        include_processing=input_path is not None and input_path.suffix == ".pdf",
                        runtime_controls={"model_budget_rmb": str(budget_rmb) if budget_rmb is not None else None,
                                          "model_max_attempts": max_attempts,
                                          "sources": {"model_budget_rmb": "NOVELTY_RUN_MODEL_BUDGET_RMB",
                                                      "model_max_attempts": "NOVELTY_RUN_MODEL_MAX_ATTEMPTS or default 80"}})
        return build_standard_full_workflow(run_config, output_root=output_root)

    budget_rmb = Decimal(os.environ["NOVELTY_RUN_MODEL_BUDGET_RMB"]) if os.getenv("NOVELTY_RUN_MODEL_BUDGET_RMB") else None
    max_attempts = (None if os.getenv("NOVELTY_RUN_MODEL_MAX_ATTEMPTS", "80").strip().lower() == "none"
                    else int(os.getenv("NOVELTY_RUN_MODEL_MAX_ATTEMPTS", "80")))
    return NoveltyWorkflowService(
        workflow_factory=workflow_factory,
        processor=processor,
        runs_root=settings.runs_root,
        max_upload_bytes=settings.max_upload_bytes,
        run_model_budget_rmb=budget_rmb,
        run_model_max_attempts=max_attempts,
    )


def build_demo_workflow_service(settings: NoveltyWebSettings) -> NoveltyWorkflowService:
    from novelty_agent_framework.processing import DefaultPaperProcessor

    return NoveltyWorkflowService(
        workflow_factory=lambda output_root: NoveltyWorkflow.default(
            output_root=output_root
        ),
        processor=DefaultPaperProcessor(parser="text_layer"),
        runs_root=settings.runs_root,
        max_upload_bytes=settings.max_upload_bytes,
    )
