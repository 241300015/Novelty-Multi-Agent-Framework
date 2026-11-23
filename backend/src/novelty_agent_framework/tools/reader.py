"""Agent-facing Reader tool built on the deterministic artifact reader."""

from __future__ import annotations

import asyncio
from base64 import b64encode
import time
import re
from typing import TypeAlias
from pydantic import Field, create_model

from ..schemas import (
    ReaderArguments,
    ArtifactNamespace,
    NoveltyPointReviewRequest,
    ReferenceReadRequest,
    ResearcherToolObservation,
    TaskResearchRequest,
)
from .reference_reader import ReferenceArtifactReaderTool
from ..persistence import reference_store_for_artifact_namespace
from ..schemas.research_tools import ReaderCallArguments
from ..core.failure_classification import material_failure_code
from ..schemas.failures import FailureCode, FailureScope, make_failure


from ..schemas.reader import ReaderIssue, ReaderOpen, RecordReaderCall
from ..schemas.materials import MaterialScope
from .full_text_reader import FullTextReader
from .record_reader import RecordReader


class ReaderTool:
    """Expose bounded Artifact reads to a Researcher agent."""

    name = "reader"
    description = ('按 Artifact ID 读取可验证文本。发现多个候选后优先一次批量读取必要片段：'
                   'reads=[{"artifact_id":"...","max_chars":2000}, ...]，最多4段；'
                   '每段保留独立 read_id、来源和字符范围。也支持单个 artifact_id。')
    description += ' 候选文献按 candidate 的 source_record_id 和 part（abstract 或 full_text）读取。'
    args_schema = RecordReaderCall

    def __init__(
        self,
        reader: ReferenceArtifactReaderTool,
        *,
        default_chars_per_read: int | None = None,
    ) -> None:
        self.reader = reader
        resolved_default = min(8_000, reader.max_chars_per_read) if default_chars_per_read is None else default_chars_per_read
        if not 1 <= resolved_default <= reader.max_chars_per_read:
            raise ValueError("default_chars_per_read exceeds reader limit")
        self.default_chars_per_read = resolved_default
        item_schema = create_model(
            f"ConfiguredReaderArguments{resolved_default}_{reader.max_chars_per_read}",
            __base__=ReaderArguments,
            max_chars=(int, Field(default=resolved_default, ge=1, le=reader.max_chars_per_read)),
        )
        self.args_schema = create_model(
            f"ConfiguredReaderCall{resolved_default}_{reader.max_chars_per_read}",
            __base__=type(self).args_schema,
            max_chars=(int, Field(default=resolved_default, ge=1, le=reader.max_chars_per_read)),
            reads=(list[item_schema] | None, Field(default=None, min_length=1, max_length=4)),
        )

    async def ainvoke(
        self,
        arguments: ReaderArguments,
        *,
        scope: TaskResearchRequest,
    ) -> ResearcherToolObservation:
        candidate = getattr(arguments, "candidate", None)
        if candidate is not None:
            service = RecordReader(self.reader.reference_store, self.reader.max_chars_per_read)
            result = await asyncio.to_thread(service.candidate_page, scope.subject_paper_id, candidate, arguments.max_chars)
            issue = isinstance(result, ReaderIssue)
            return ResearcherToolObservation(
                tool_name=self.name, arguments=arguments.model_dump(mode="json"), succeeded=not issue,
                summary=result.status if issue else "candidate text", error=result.message if issue else None,
                payload={"reader_issue": result.model_dump(mode="json")} if issue else result)
        request = getattr(arguments, "request", None)
        if request is not None:
            service = RecordReader(self.reader.reference_store, self.reader.max_chars_per_read)
            operation = service.open if isinstance(request, ReaderOpen) else service.read
            result = await asyncio.to_thread(operation, scope.subject_paper_id, request)
            return ResearcherToolObservation(
                tool_name=self.name, arguments={"request": request.model_dump(mode="json", exclude_none=True)},
                succeeded=not isinstance(result, ReaderIssue), summary=result.status,
                error=result.message if isinstance(result, ReaderIssue) else None,
                payload={"record_result": result.model_dump(mode="json")},
            )
        if getattr(arguments, "reads", None) is not None:
            return await self._read_batch(arguments, scope=scope)
        started = time.monotonic()
        result = await self.reader.ainvoke(
            ReferenceReadRequest(
                subject_paper_id=scope.subject_paper_id,
                namespace=self._namespace_for(scope.subject_paper_id, arguments),
                artifact_id=arguments.artifact_id,
                char_start=arguments.char_start,
                max_chars=arguments.max_chars,
            )
        )
        return ResearcherToolObservation(
            tool_name=self.name,
            arguments=arguments.model_dump(mode="json", exclude_none=True),
            succeeded=True,
            summary=(
                f"读取 artifact {result.artifact_id} 字符 "
                f"[{result.char_start}, {result.char_end})"
            ),
            payload={"read_result": result.model_dump(mode="json"),
                     "read_status": "content" if result.char_end > result.char_start else "artifact_eof"},
            elapsed_ms=int((time.monotonic() - started) * 1000),
        )

    async def _read_batch(self, arguments, *, scope) -> ResearcherToolObservation:
        started = time.monotonic()
        results, errors, statuses, issues = [], [], [], []
        # Local reads are bounded; batching removes model round trips, without
        # weakening the single-tool policy or creating unbounded I/O concurrency.
        for index, request in enumerate(arguments.reads):
            try:
                observation = await self.ainvoke(request, scope=scope)
                issues.extend(observation.payload.get("execution_issues", []))
                if observation.succeeded:
                    results.append(observation.payload["read_result"])
                    statuses.append(observation.payload.get("read_status", "content"))
                else:
                    errors.append({"index": index, "artifact_id": request.artifact_id,
                                   "error_type": "ReaderUnavailable", "message": observation.error or "read failed"})
            except Exception as exc:
                errors.append({"index": index, "artifact_id": request.artifact_id,
                               "error_type": type(exc).__name__, "message": str(exc)[:500]})
        return ResearcherToolObservation(
            tool_name=self.name, arguments=arguments.model_dump(mode="json"),
            succeeded=bool(results),
            summary=f"批量读取：成功 {len(results)} 段，失败 {len(errors)} 段",
            payload={"read_results": results, "read_errors": errors,
                     "read_statuses": statuses,
                     **({"execution_issues": issues} if issues else {}),
                     **({"read_status": "artifact_eof"} if statuses and
                        all(status == "artifact_eof" for status in statuses) else {})},
            elapsed_ms=int((time.monotonic() - started) * 1000),
        )

    def _namespace_for(
        self, subject_paper_id: str, arguments: ReaderArguments
    ) -> ArtifactNamespace:
        """按制品实际归属判定命名空间。

        模型只转述工具给出的 artifact_id，无法判断它属于研究语料还是论文自带
        参考语料（实测唯一的失败原因就是这里被默认成了 research_reference），
        所以归属由工具查 Manifest 决定，而不是让模型猜。
        """

        namespace = self.reader.locate(subject_paper_id, arguments.artifact_id)
        if namespace is None:
            raise ValueError(
                f"unknown artifact_id {arguments.artifact_id!r} in the research or "
                "subject reference manifest"
            )
        return namespace

    def model_image(self, observation, scope):
        """Hydrate verified object pixels only for a successful scoped reader result."""
        from backend.env import ImageContentPart
        from ..material_store import MaterialStore
        from ..schemas.reader import IndexedFullTextHandle, ObjectImage

        result = observation.payload.get("record_result", {})
        content = result.get("content")
        if not observation.succeeded or not isinstance(content, dict) or content.get("kind") != "image":
            return None
        request = observation.arguments["request"]
        handle = IndexedFullTextHandle.model_validate(request["handle"])
        if (handle.subject_paper_id != scope.subject_paper_id
                or request["object_id"] != result["object"]["object_id"]):
            raise ValueError("object image does not belong to this read")
        image = ObjectImage.model_validate(content)
        body = MaterialStore(self.reader.reference_store).object_png(handle, image)
        return ImageContentPart(image_url="data:image/png;base64," + b64encode(body).decode("ascii"))

    def project_model_context(
        self, observation: ResearcherToolObservation
    ) -> dict[str, object]:
        if "record_result" in observation.payload:
            return observation.payload["record_result"]
        if not observation.succeeded and "read_results" not in observation.payload:
            return {"succeeded": False, "error": observation.error,
                    "execution_issues": observation.payload.get("execution_issues", [])}
        if "read_results" in observation.payload:
            return {"succeeded": observation.succeeded, "summary": observation.summary,
                    "read_results": observation.payload["read_results"],
                    "read_errors": observation.payload["read_errors"],
                    "read_statuses": observation.payload.get("read_statuses", []),
                    **({"read_status": observation.payload["read_status"]}
                       if "read_status" in observation.payload else {})}
        read = observation.payload["read_result"]
        return {
            "succeeded": observation.succeeded,
            "summary": observation.summary,
            **({"read_status": observation.payload["read_status"],
                "material_catalog": observation.payload.get("material_catalog", [])}
               if "read_status" in observation.payload else {}),
            "read_result": {
                key: read[key]
                for key in (
                    "namespace",
                    "read_id",
                    "work_id",
                    "artifact_id",
                    "role",
                    "char_start",
                    "char_end",
                    "text",
                    "has_more",
                )
            },
        }


class ReviewerReaderTool(ReaderTool):
    """Read verified materials belonging to the Card's bound Work."""

    args_schema = ReaderCallArguments
    description = "仅回读当前 Card 所绑定 Work 的已验证材料，支持 artifact_id 或 reads 批量读取。"

    def material_catalog(self, scope: NoveltyPointReviewRequest) -> list[dict[str, object]]:
        """Resolve both the displayed catalog and the permission set from one source."""
        return self._material_catalog(scope, {})

    def _material_catalog(self, scope, pending):
        cited = {eid for card in scope.cards for eid in card.evidence_ids}
        bound = [item for item in scope.evidence if item.evidence_id in cited]
        addresses = {(item.work_id,
                      ArtifactNamespace(item.provenance.get("artifact_namespace", "research_reference")))
                     for item in bound}
        if not addresses:
            return []
        entries: list[dict[str, object]] = []
        root = getattr(getattr(self.reader, "reference_store", None), "output_root", None)
        if root is not None:
            for work_id, namespace in sorted(addresses, key=lambda x: (x[0], x[1].value)):
                store = reference_store_for_artifact_namespace(namespace, output_root=root)
                try:
                    manifest = store.load_manifest(scope.subject_paper_id)
                except (OSError, ValueError) as exc:
                    for item in bound:
                        if item.work_id == work_id and item.provenance.get("artifact_namespace", "research_reference") == namespace.value:
                            entries.append({"work_id": work_id, "artifact_id": item.artifact_id,
                                "namespace": namespace.value, "source_record_id": item.provenance.get("source_record_id"),
                                "role": "unknown", "content_extent": "unknown", "content_hash": None,
                                "readable_chars": None, "version_label": None, "readable": False,
                                "availability_code": material_failure_code(exc, phase="manifest").value})
                    continue
                work_evidence = [e for e in bound if e.work_id == work_id and
                                 e.provenance.get("artifact_namespace", "research_reference") == namespace.value]
                cited_ids = {e.artifact_id for e in work_evidence}
                cited_artifacts = [a for a in manifest.artifacts
                                   if a.work_id == work_id and a.artifact_id in cited_ids]
                # Opening a new original requires the cited artifact's persisted record binding.
                cited_records = {a.source_record_id for a in cited_artifacts if a.source_record_id}
                cited_versions = {a.version_label for a in cited_artifacts if a.version_label}
                selections = {}
                registered = {a.artifact_id for a in manifest.artifacts}
                originals = [a for a in manifest.artifacts if a.work_id == work_id
                             and a.source_record_id in cited_records and a.provenance.get("material_representation") == "raw"]
                if originals:
                    material_scope = MaterialScope(subject_paper_id=scope.subject_paper_id, namespace=namespace)
                    try:
                        selections = FullTextReader(store).text_selections(material_scope, manifest, cited_records)
                    except (OSError, ValueError):
                        # The original registry cannot authorize a new text address.
                        # Existing evidence remains available for verification below.
                        selections = {}
                for artifact in manifest.artifacts:
                    if artifact.work_id != work_id:
                        continue
                    representation = artifact.provenance.get("material_representation")
                    if representation in {"raw", "parsed"} and artifact.source_record_id not in cited_records:
                        continue
                    if cited_versions and artifact.version_label and artifact.version_label not in cited_versions:
                        continue
                    if representation == "raw":
                        text_id = selections.get(artifact.artifact_id)
                        if text_id and text_id not in registered:
                            pending[(namespace, text_id)] = artifact
                            entries.append({"work_id": work_id, "artifact_id": text_id,
                                "namespace": namespace.value, "source_record_id": artifact.source_record_id,
                                "role": "extracted_text", "content_extent": "unknown", "content_hash": None,
                                "readable_chars": None, "version_label": artifact.version_label, "readable": True})
                        continue
                    if artifact.media_type not in {
                        "text/plain", "text/markdown", "text/html", "application/json",
                    }:
                        continue
                    try:
                        _, _, raw = store.verify_artifact_file(scope.subject_paper_id, artifact.artifact_id)
                        content = raw.decode("utf-8")
                        chars = len(content)
                    except (OSError, ValueError, UnicodeDecodeError) as exc:
                        entries.append({"work_id": work_id, "artifact_id": artifact.artifact_id,
                            "namespace": namespace.value, "source_record_id": artifact.source_record_id,
                            "role": artifact.role.value, "content_extent": artifact.content_extent.value,
                            "content_hash": artifact.sha256, "readable_chars": None,
                            "version_label": artifact.version_label, "readable": False,
                            "availability_code": material_failure_code(exc).value})
                        continue
                    entries.append({
                        "work_id": work_id, "artifact_id": artifact.artifact_id,
                        "namespace": namespace.value, "source_record_id": artifact.source_record_id,
                        "role": artifact.role.value, "content_extent": artifact.content_extent.value,
                        "content_hash": artifact.sha256, "readable_chars": chars,
                        "version_label": artifact.version_label, "readable": True,
                        "section_hints": [
                            {"char_start": match.start(), "heading": match.group().strip()}
                            for match in list(re.finditer(
                                r"(?im)^(?:\d+(?:\.\d+)*[. ]+)?[^\n]{0,45}"
                                r"(?:method|methods|approach|algorithm|related work|方法|算法|模型)"
                                r"[^\n]{0,40}$", content))[:12]
                        ],
                    })
        if root is not None:
            # A previously bound Artifact disappearing from its manifest is a
            # material failure. It is never permission to read an arbitrary ID.
            known = {(entry["namespace"], entry["artifact_id"]) for entry in entries}
            for item in bound:
                namespace = item.provenance.get("artifact_namespace", "research_reference")
                if (namespace, item.artifact_id) not in known:
                    entries.append({"work_id": item.work_id, "artifact_id": item.artifact_id,
                        "namespace": namespace, "source_record_id": item.provenance.get("source_record_id"),
                        "role": "unknown", "content_extent": "unknown", "content_hash": None,
                        "readable_chars": None, "version_label": None, "readable": False,
                        "availability_code": FailureCode.READER_ARTIFACT_UNREGISTERED.value})
        # Test adapters without a manifest retain the old evidence-only scope.
        if root is None:
            for item in bound:
                entries.append({"work_id": item.work_id, "artifact_id": item.artifact_id,
                    "namespace": item.provenance.get("artifact_namespace", "research_reference"),
                    "source_record_id": item.provenance.get("source_record_id"),
                    "role": "unknown", "content_extent": "unknown", "content_hash": None,
                    "readable_chars": None, "version_label": None, "readable": True})
        return list({(e["namespace"], e["artifact_id"]): e for e in entries}.values())

    async def ainvoke(
        self,
        arguments: ReaderArguments,
        *,
        scope: NoveltyPointReviewRequest,
    ) -> ResearcherToolObservation:
        if getattr(arguments, "reads", None) is not None:
            return await self._read_batch(arguments, scope=scope)
        pending = {}
        catalog = self._material_catalog(scope, pending)
        matches = [item for item in catalog if item["artifact_id"] == arguments.artifact_id]
        if len(matches) != 1:
            raise PermissionError(
                f"artifact {arguments.artifact_id!r} is outside reviewer scope"
            )
        entry = matches[0]
        if not entry.get("readable", True):
            code = FailureCode(entry["availability_code"])
            issue = make_failure(code, scope=FailureScope(node="reader.artifact", paper_id=scope.subject_paper_id,
                point_id=scope.novelty_point.point_id, artifact_id=arguments.artifact_id),
                message="Authorized Reviewer material is unavailable or failed integrity verification")
            return ResearcherToolObservation(tool_name=self.name,
                arguments=arguments.model_dump(mode="json"), succeeded=False,
                error=issue.message, payload={"execution_issues": [issue.model_dump(mode="json")]})
        namespace, work_id = ArtifactNamespace(entry["namespace"]), entry["work_id"]
        started = time.monotonic()
        original = pending.get((namespace, arguments.artifact_id))
        if original is not None:
            store = reference_store_for_artifact_namespace(namespace, output_root=self.reader.reference_store.output_root)
            service = RecordReader(store, self.reader.max_chars_per_read)
            opened = await asyncio.to_thread(service.open, scope.subject_paper_id, ReaderOpen(
                operation="open", part="full_text", source_record_id=original.source_record_id),
                raw_id=original.artifact_id)
            if isinstance(opened, ReaderIssue):
                code = (FailureCode.READER_DECODE_FAILED if opened.status == "parse_failed"
                        else FailureCode.READER_VERIFICATION_FAILED)
                issue = make_failure(code, scope=FailureScope(node="reader.artifact", paper_id=scope.subject_paper_id,
                    point_id=scope.novelty_point.point_id, artifact_id=arguments.artifact_id), message=opened.message)
                return ResearcherToolObservation(tool_name=self.name, arguments=arguments.model_dump(mode="json"),
                    succeeded=False, error=issue.message, payload={"execution_issues": [issue.model_dump(mode="json")]})
            if (opened.handle.parsed_id != arguments.artifact_id or opened.handle.work_id != work_id
                    or opened.handle.source_record_id != original.source_record_id
                    or opened.handle.sha256 != original.sha256):
                raise PermissionError("opened text differs from authorized Reviewer selection")
            await asyncio.to_thread(service.register_full_text, scope.subject_paper_id, opened.handle)
        result = await self.reader.ainvoke(
            ReferenceReadRequest(
                subject_paper_id=scope.subject_paper_id,
                namespace=namespace,
                artifact_id=arguments.artifact_id,
                char_start=arguments.char_start,
                max_chars=arguments.max_chars,
            )
        )
        if (result.artifact_id != arguments.artifact_id
                or result.namespace != namespace or result.work_id != work_id
                or (entry["content_hash"] is not None and result.sha256 != entry["content_hash"])):
            raise PermissionError("reader returned an artifact outside reviewer scope")
        read_status = "content" if result.char_end > result.char_start else "artifact_eof"
        return ResearcherToolObservation(
            tool_name=self.name,
            arguments=arguments.model_dump(mode="json", exclude_none=True),
            succeeded=True,
            summary=(
                f"读取 artifact {result.artifact_id} 字符 [{result.char_start}, {result.char_end})；"
                + ("当前制品 EOF，可查材料目录中的其他已授权制品" if read_status == "artifact_eof" else "有内容")
            ),
            payload={"read_result": result.model_dump(mode="json"), "read_status": read_status,
                     "material_catalog": catalog if read_status == "artifact_eof" else []},
            elapsed_ms=int((time.monotonic() - started) * 1000),
        )


# Transitional import compatibility. This is the same implementation, not a
# second tool, and therefore exposes the canonical ``reader`` tool name.
ReferenceReaderResearcherTool: TypeAlias = ReaderTool
