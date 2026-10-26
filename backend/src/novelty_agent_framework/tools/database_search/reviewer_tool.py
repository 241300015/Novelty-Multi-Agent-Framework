"""Reviewer-only adapter for bounded full-text acquisition."""

from __future__ import annotations

from ...schemas import (
    ArtifactNamespace,
    DatabaseSearchArguments,
    FullTextAcquisitionRequest,
    NoveltyPointReviewRequest,
    SourceKind,
)
from .tool import DatabaseSearchTool


class ReviewerDatabaseSearchTool:
    """Expose acquisition for records already cited by exactly one Review Card."""

    name = "database_search"
    args_schema = DatabaseSearchArguments
    description = (
        "仅为当前单卡已绑定的结构化数据库记录按需补取全文；"
        "必须提供 full_text_source_record_ids，不执行新检索、不扩展候选。"
        "只能请求当前 Card 的 Evidence provenance.source_record_id，获取后必须用 reader 回读。"
    )

    def __init__(self, database_search: DatabaseSearchTool) -> None:
        self.database_search = database_search
        self.reference_store = database_search.reference_store

    async def ainvoke(
        self,
        arguments: DatabaseSearchArguments,
        *,
        scope: NoveltyPointReviewRequest,
    ):
        if len(scope.cards) != 1:
            raise PermissionError("reviewer full-text acquisition requires exactly one Card")
        if not arguments.full_text_source_record_ids:
            raise PermissionError("reviewer database_search cannot execute discovery")

        card = scope.cards[0]
        tasks = [task for task in scope.tasks if task.task_id == card.task_id]
        if len(tasks) != 1 or tasks[0].novelty_point_id != scope.novelty_point.point_id:
            raise PermissionError("reviewer Card has no unique task binding")

        cited = set(card.evidence_ids)
        evidence = [item for item in scope.evidence if item.evidence_id in cited]
        if len(evidence) != len(cited):
            raise PermissionError("reviewer Card Evidence binding is incomplete")
        allowed: dict[str, str] = {}
        for item in evidence:
            namespace = item.provenance.get(
                "artifact_namespace", ArtifactNamespace.RESEARCH_REFERENCE.value
            )
            record_id = item.provenance.get("source_record_id")
            if (
                namespace != ArtifactNamespace.RESEARCH_REFERENCE.value
                or not isinstance(record_id, str)
                or not record_id
                or item.task_id != card.task_id
                or item.novelty_point_id != card.novelty_point_id
            ):
                continue
            previous = allowed.setdefault(record_id, item.work_id)
            if previous != item.work_id:
                raise PermissionError("source record is bound to conflicting Works")

        requested = list(dict.fromkeys(arguments.full_text_source_record_ids))
        if not requested or any(record_id not in allowed for record_id in requested):
            raise PermissionError("requested source record is outside the current Card")

        manifest = self.reference_store.load_manifest(scope.subject_paper_id)
        records = {item.source_record_id: item for item in manifest.source_records}
        for record_id in requested:
            record = records.get(record_id)
            if (
                record is None
                or record.source_id != arguments.source_id
                or record.source_kind != SourceKind.STRUCTURED_DATABASE
                or record.work_id != allowed[record_id]
            ):
                raise PermissionError("requested source record failed manifest binding")

        acquisition_scope = FullTextAcquisitionRequest(
            subject_paper_id=scope.subject_paper_id,
            run_id=scope.run_id,
            source_id=arguments.source_id,
            novelty_point=scope.novelty_point,
            research_task=tasks[0],
        )
        return await self.database_search.acquire_full_texts(
            arguments.model_copy(update={"full_text_source_record_ids": requested}),
            scope=acquisition_scope,
        )

    def project_model_context(self, observation):
        return self.database_search.project_model_context(observation)
