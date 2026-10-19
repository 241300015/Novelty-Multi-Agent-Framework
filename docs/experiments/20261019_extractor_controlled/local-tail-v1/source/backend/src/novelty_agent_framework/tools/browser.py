"""Handle-based Browser tool over a provider-neutral acquisition backend."""

from __future__ import annotations

import hashlib
import time
from datetime import datetime, timezone

from ..persistence import ReferenceStore
from ..schemas import (
    SourceKind,
    AccessStatus,
    Artifact,
    ArtifactRole,
    BrowserArguments,
    BrowserArtifactItem,
    BrowserResult,
    ContentExtent,
    ResearcherToolObservation,
    TaskResearchRequest,
    Work,
    WorkType,
)
from .browser_backend import BrowserBackend


class BrowserTool:
    name = "browser"
    description = "按 SourceRecord 句柄获取网页正文并保存为可读取 Artifact。"
    args_schema = BrowserArguments

    def __init__(
        self,
        backend: BrowserBackend,
        reference_store: ReferenceStore | None = None,
    ) -> None:
        if not backend.name.strip():
            raise ValueError("browser backend name cannot be empty")
        self.backend = backend
        self.reference_store = reference_store or ReferenceStore()

    async def ainvoke(
        self,
        arguments: BrowserArguments,
        *,
        scope: TaskResearchRequest,
    ) -> ResearcherToolObservation:
        started = time.monotonic()
        manifest = self.reference_store.load_manifest(scope.subject_paper_id)
        record_index = next(
            (
                index
                for index, record in enumerate(manifest.source_records)
                if record.source_record_id == arguments.source_record_id
            ),
            None,
        )
        if record_index is None:
            raise ValueError(f"unknown source_record_id {arguments.source_record_id!r}")
        record = manifest.source_records[record_index]
        trusted_url = record.full_text_url or record.landing_url
        if trusted_url is None:
            raise ValueError(
                f"source_record {record.source_record_id!r} has no fetchable URL"
            )

        fetched = await self.backend.fetch(trusted_url)
        acquired_at = datetime.now(timezone.utc)
        work_id = record.work_id or _work_id(record.source_record_id)
        works_by_id = {work.work_id: work for work in manifest.works}
        if work_id not in works_by_id:
            works_by_id[work_id] = Work(
                work_id=work_id,
                work_type=WorkType.WEBPAGE,
                title=fetched.title or record.title,
                canonical_source_record_id=record.source_record_id,
            )

        content_sha256 = hashlib.sha256(fetched.text.encode("utf-8")).hexdigest()
        artifact_id = _artifact_id(
            record.source_record_id,
            content_sha256,
            ArtifactRole.EXTRACTED_TEXT,
        )
        artifact = Artifact(
            artifact_id=artifact_id,
            work_id=work_id,
            source_record_id=record.source_record_id,
            role=ArtifactRole.EXTRACTED_TEXT,
            media_type="text/plain",
            relative_path=f"documents/{work_id}/{artifact_id}.txt",
            sha256=content_sha256,
            byte_size=len(fetched.text.encode("utf-8")),
            content_extent=ContentExtent.FULL,
            acquired_at=acquired_at,
            extraction_warnings=list(fetched.warnings),
            provenance={
                "tool": self.name,
                "source_kind": "web_supplement",
                "content_origin": "source_text",
                "backend": self.backend.name,
                "run_id": scope.run_id,
                "requested_url": fetched.requested_url,
                "final_url": fetched.final_url,
                "content_type": fetched.content_type,
                "browser_metadata": fetched.metadata,
            },
        )
        self.reference_store.write_document(
            scope.subject_paper_id,
            work_id=work_id,
            artifact_id=artifact_id,
            extension="txt",
            content=fetched.text,
        )
        updated_record = record.model_copy(
            update={
                "work_id": work_id,
                "access_status": AccessStatus.FULL_TEXT_ACQUIRED,
                "source_kind": SourceKind.WEB_SUPPLEMENT,
            }
        )
        # 在锁内重新读取再合并：fetch 期间其它并发任务可能已经写入 manifest，
        # 用 fetch 之前读到的副本整份覆盖会丢掉它们的 Work/SourceRecord/Artifact。
        self.reference_store.merge_manifest(
            scope.subject_paper_id,
            works=list(works_by_id.values()),
            source_records=[updated_record],
            artifacts=[artifact],
        )
        result = BrowserResult(
            source_record_id=record.source_record_id,
            work_id=work_id,
            artifacts=[
                BrowserArtifactItem(
                    artifact_id=artifact_id,
                    role=artifact.role,
                    media_type=artifact.media_type,
                    content_extent=artifact.content_extent,
                )
            ],
            warnings=list(fetched.warnings),
        )
        return ResearcherToolObservation(
            tool_name=self.name,
            arguments=arguments.model_dump(mode="json"),
            succeeded=True,
            summary=f"获取网页并保存 {len(result.artifacts)} 个 Artifact",
            payload={
                "browser_result": result.model_dump(mode="json"),
                "browser_fetch": fetched.model_dump(mode="json"),
            },
            elapsed_ms=int((time.monotonic() - started) * 1_000),
        )

    def project_model_context(
        self, observation: ResearcherToolObservation
    ) -> dict[str, object]:
        result = observation.payload["browser_result"]
        return {
            "succeeded": observation.succeeded,
            "summary": observation.summary,
            **result,
        }


def _work_id(source_record_id: str) -> str:
    digest = hashlib.sha256(source_record_id.encode("utf-8")).hexdigest()
    return f"wrk_{digest[:24]}"


def _artifact_id(
    source_record_id: str,
    content_sha256: str,
    role: ArtifactRole,
) -> str:
    identity = f"{source_record_id}\0{content_sha256}\0{role.value}"
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    return f"art_{digest[:24]}"
