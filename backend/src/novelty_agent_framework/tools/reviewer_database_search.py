"""Reviewer adapter: known-paper acquisition authorized after a local read attempt."""
from __future__ import annotations

from dataclasses import dataclass

from ..core.runtime_artifacts import current_runtime_artifacts
from ..schemas import NoveltyPointReviewRequest
from ..schemas.references import FullTextAcquisitionRequest, SourceKind, ArtifactRole
from .database_search import DatabaseSearchTool


@dataclass(frozen=True)
class ReviewerAcquisitionScope:
    """Runtime-owned permission; never part of model-written tool arguments."""

    request: NoveltyPointReviewRequest
    source_id: str
    source_record_id: str
    local_attempt_artifact_ids: tuple[str, ...]

    @property
    def subject_paper_id(self):
        return self.request.subject_paper_id

    @property
    def novelty_point(self):
        return self.request.novelty_point


class ReviewerDatabaseSearchTool(DatabaseSearchTool):
    """Keep the canonical database_search name, with an acquisition-only boundary."""

    def _discovery_sources(self, scope):
        raise PermissionError("Reviewer cannot discover new papers")

    def __init__(self, database: DatabaseSearchTool):
        super().__init__(database.tools_by_source, database.reference_store)
        self.description = (
            "仅在本地回读尝试后获取当前 Card 已绑定文献的全文；不执行新检索。"
            "source_id 和 full_text_source_record_ids 由 Harness 当前选项解析，"
            "获取后必须经 reader 实际读取，才能引用原文。")

    def acquisition_catalog(self, request, *, local_attempt_artifact_ids):
        if len(request.cards) != 1:
            return []
        card = request.cards[0]
        tasks = [t for t in request.tasks if t.task_id == card.task_id
                 and t.novelty_point_id == request.novelty_point.point_id]
        if len(tasks) != 1:
            return []
        manifest = self.reference_store.load_manifest(request.subject_paper_id)
        artifacts = {a.artifact_id: a for a in manifest.artifacts}
        records = {r.source_record_id: r for r in manifest.source_records}
        bound = [e for e in request.evidence if e.evidence_id in card.evidence_ids
                 and e.provenance.get("artifact_namespace", "research_reference") == "research_reference"]
        attempted_works = {artifacts[aid].work_id for aid in local_attempt_artifact_ids if aid in artifacts}
        candidates = {}
        for evidence in bound:
            artifact = artifacts.get(evidence.artifact_id)
            if artifact is None or artifact.work_id != evidence.work_id or artifact.work_id not in attempted_works:
                continue
            record = records.get(artifact.source_record_id)
            retrieval = self.tools_by_source.get(record.source_id) if record else None
            if (record is None or record.work_id != artifact.work_id or not record.external_id
                    or record.source_kind != SourceKind.STRUCTURED_DATABASE or retrieval is None
                    or retrieval.source.full_text_tool is None):
                continue
            # A Manifest entry alone is not a usable local full text.
            ready = False
            for body in manifest.artifacts:
                if body.source_record_id != record.source_record_id or body.role != ArtifactRole.EXTRACTED_TEXT:
                    continue
                try:
                    _, _, raw = self.reference_store.verify_artifact_file(request.subject_paper_id, body.artifact_id)
                    ready = bool(raw.decode("utf-8").strip())
                except (OSError, ValueError, UnicodeDecodeError):
                    continue
                if ready:
                    break
            if not ready:
                candidates[record.source_record_id] = {
                    "source_id": record.source_id, "source_record_id": record.source_record_id,
                    "work_id": record.work_id, "title": record.title}
        return list(candidates.values())

    def _request(self, arguments, scope):
        if not isinstance(scope, ReviewerAcquisitionScope) or not scope.local_attempt_artifact_ids:
            raise PermissionError("Reviewer acquisition requires a runtime-owned local-read attempt")
        if (arguments.source_id != scope.source_id
                or arguments.full_text_source_record_ids != [scope.source_record_id]):
            raise PermissionError("Reviewer cannot discover papers or change the authorized source record")
        # Recheck persisted bindings at dispatch, independently of the menu.
        candidates = self.acquisition_catalog(scope.request,
            local_attempt_artifact_ids=scope.local_attempt_artifact_ids)
        if not any(c["source_id"] == scope.source_id and c["source_record_id"] == scope.source_record_id
                   for c in candidates):
            raise PermissionError("Reviewer acquisition source is unavailable, already local, or outside Card scope")
        task = next(t for t in scope.request.tasks if t.task_id == scope.request.cards[0].task_id)
        runtime = current_runtime_artifacts()
        return FullTextAcquisitionRequest(subject_paper_id=scope.subject_paper_id,
            source_id=scope.source_id, novelty_point=scope.novelty_point, research_task=task,
            run_id=getattr(runtime, "run_id", None))
