"""Agent-facing Reader tool built on the deterministic artifact reader."""

from __future__ import annotations

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


class ReaderTool:
    """Expose bounded Artifact reads to a Researcher agent."""

    name = "reader"
    description = ('按 Artifact ID 读取可验证文本。发现多个候选后优先一次批量读取必要片段：'
                   'reads=[{"artifact_id":"...","max_chars":2000}, ...]，最多4段；'
                   '每段保留独立 read_id、来源和字符范围。也支持单个 artifact_id。')
    args_schema = ReaderCallArguments

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
            __base__=ReaderCallArguments,
            max_chars=(int, Field(default=resolved_default, ge=1, le=reader.max_chars_per_read)),
            reads=(list[item_schema] | None, Field(default=None, min_length=1, max_length=4)),
        )

    async def ainvoke(
        self,
        arguments: ReaderArguments,
        *,
        scope: TaskResearchRequest,
    ) -> ResearcherToolObservation:
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

    def project_model_context(
        self, observation: ResearcherToolObservation
    ) -> dict[str, object]:
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

    description = "仅回读当前 Card 所绑定 Work 的已验证材料。" + ReaderTool.description

    def material_catalog(self, scope: NoveltyPointReviewRequest) -> list[dict[str, object]]:
        """Resolve both the displayed catalog and the permission set from one source."""
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
                cited_versions = {
                    artifact.version_label for artifact in manifest.artifacts
                    if artifact.artifact_id in {item.artifact_id for item in bound if item.work_id == work_id}
                    and artifact.version_label is not None
                }
                for artifact in manifest.artifacts:
                    if artifact.work_id != work_id or artifact.media_type not in {
                        "text/plain", "text/markdown", "text/html", "application/json",
                    }:
                        continue
                    if cited_versions and artifact.version_label is not None \
                            and artifact.version_label not in cited_versions:
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
        catalog = self.material_catalog(scope)
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
