"""Offline, record-addressed reads over provider-owned manifest fields."""
from hashlib import sha256

from ..persistence import ReferenceStore, artifact_namespace_for_reference_namespace
from ..schemas.materials import JsonPointerLocator, canonical_material_json
from ..schemas.reader import (
    AbstractLocator, OpenedRecord, ReaderBlock, ReaderCursor, ReaderHandle, ReaderIssue,
    ReaderOpen, ReaderRead, RecordReadResult,
)
from ..schemas.materials import MaterialScope
from ..schemas.reader import FullTextHandle
from ..schemas.references import Artifact, ArtifactRole, ContentExtent
from .full_text_reader import FullTextReader


def _digest(value):
    return sha256(canonical_material_json(value)).hexdigest()


class RecordReader:
    def __init__(self, store: ReferenceStore, max_chars: int):
        self.store = store
        self.max_chars = max_chars

    def _manifest(self, paper_id):
        manifest = self.store.load_manifest(paper_id)
        if manifest.subject_paper_id != paper_id:
            raise ValueError("source manifest belongs to another paper")
        return manifest

    def open(self, paper_id: str, request: ReaderOpen, *, raw_id=None) -> OpenedRecord | ReaderIssue:
        manifest = self._manifest(paper_id)
        record = next((r for r in manifest.source_records if r.source_record_id == request.source_record_id), None)
        if record is not None and request.part == "full_text":
            scope = MaterialScope(subject_paper_id=paper_id,
                                  namespace=artifact_namespace_for_reference_namespace(self.store.namespace))
            try:
                return FullTextReader(self.store).open(scope, manifest, record, request.source_format, raw_id=raw_id)
            except (OSError, ValueError):
                return ReaderIssue(status="integrity_error", message="Registered material could not be verified.")
        if request.part != "abstract" or not record or not (record.abstract or "").strip():
            return ReaderIssue(status="unavailable", message="Requested material is unavailable.")
        return OpenedRecord(handle=self._handle(paper_id, record))

    def candidate_page(self, paper_id, candidate, limit):
        """Resolve a menu selection and return evidence-compatible text plus private replay data."""
        if candidate.continuation:
            handle = candidate.continuation.handle
            cursor = candidate.continuation.cursor
            part = "full_text" if isinstance(handle, FullTextHandle) else "abstract"
            if (handle.source_record_id != candidate.source_record_id or part != candidate.part or
                    candidate.raw_id and getattr(handle, "raw_id", None) != candidate.raw_id):
                return ReaderIssue(status="invalid_address", message="Continuation belongs to a different candidate.")
        else:
            opened = self.open(paper_id, ReaderOpen(operation="open", source_record_id=candidate.source_record_id,
                                                    part=candidate.part), raw_id=candidate.raw_id)
            if isinstance(opened, ReaderIssue):
                return opened
            handle, cursor = opened.handle, None
        result = self.read(paper_id, ReaderRead(operation="read", handle=handle, cursor=cursor, max_tokens=limit))
        if isinstance(result, ReaderIssue):
            return result
        if not handle.work_id:
            return ReaderIssue(status="unavailable", message="Candidate has no registered work for evidence.")
        if isinstance(handle, FullTextHandle):
            artifact = self.register_full_text(paper_id, handle)
        else:
            record = next(r for r in self._manifest(paper_id).source_records if r.source_record_id == handle.source_record_id)
            if not record.abstract or self._handle(paper_id, record) != handle:
                return ReaderIssue(status="stale_handle", message="Abstract changed during the read.")
            artifact = Artifact(
                artifact_id="abs_" + _digest([record.source_record_id, handle.sha256])[:24],
                work_id=handle.work_id, source_record_id=record.source_record_id, role=ArtifactRole.ABSTRACT,
                relative_path="list.json", source_field="abstract", media_type="text/plain", sha256=handle.sha256,
                byte_size=len(record.abstract.encode("utf-8")), content_extent=ContentExtent.FULL,
                acquired_at=record.observed_at,
            )
            self.store.merge_manifest(paper_id, artifacts=[artifact])
        blocks = result.blocks
        start, end = blocks[0].locator.start, blocks[-1].locator.end
        read = self.store.read_document_slice(paper_id, artifact_id=artifact.artifact_id,
                                              char_start=start, max_chars=end-start)
        if read.text != "".join(b.content for b in blocks) or read.sha256 != artifact.sha256:
            return ReaderIssue(status="integrity_error", message="Candidate blocks differ from persisted text.")
        continuation = ({"handle": handle.model_dump(mode="json"), "cursor": result.next_cursor.model_dump(mode="json")}
                        if result.next_cursor else None)
        return {"read_result": read.model_dump(mode="json"), "read_status": "content",
                "candidate_page": {"handle": handle.model_dump(mode="json"),
                    "citations": [b.model_dump(mode="json", include={"block_id", "locator"}) for b in blocks],
                    "continuation": continuation}}

    def register_full_text(self, paper_id, handle):
        """Publish the verified parsed representation for artifact-addressed reads and checkpoints."""
        from ..material_store import MaterialStore
        from ..schemas.materials import MaterialRef
        materials = MaterialStore(self.store)
        ref = MaterialRef(subject_paper_id=paper_id, namespace=handle.namespace, artifact_id=handle.parsed_id,
                          sha256=handle.parsed_sha256)
        parsed, _, _ = materials.parsed(ref)
        _, artifact = materials.legacy_artifacts(parsed)
        self.store.merge_manifest(paper_id, artifacts=[artifact])
        return artifact

    def _handle(self, paper_id, record):
        return ReaderHandle(subject_paper_id=paper_id,
                            namespace=artifact_namespace_for_reference_namespace(self.store.namespace),
                            source_record_id=record.source_record_id, work_id=record.work_id,
                            external_id=record.external_id,
                            sha256=sha256(record.abstract.encode("utf-8")).hexdigest())

    def read(self, paper_id: str, request: ReaderRead) -> RecordReadResult | ReaderIssue:
        handle = request.handle
        if (handle.subject_paper_id != paper_id or
                handle.namespace != artifact_namespace_for_reference_namespace(self.store.namespace)):
            return ReaderIssue(status="invalid_address", message="Handle belongs to another scope.")
        manifest = self._manifest(paper_id)
        record = next((r for r in manifest.source_records
                       if r.source_record_id == handle.source_record_id), None)
        if isinstance(handle, FullTextHandle) and record is not None:
            try:
                return FullTextReader(self.store).read(manifest, record, request, self.max_chars)
            except (ValueError, OSError):
                return ReaderIssue(status="integrity_error", message="Full-text bytes or mapping failed verification.")
        if request.citation and not isinstance(request.citation.locator, AbstractLocator):
            return ReaderIssue(status="invalid_address", message="Expected an abstract locator.")
        if not record or not record.abstract or self._handle(paper_id, record) != handle:
            return ReaderIssue(status="stale_handle", message="Source record or abstract snapshot changed.")
        snapshot = _digest(handle.model_dump(mode="json"))
        cursor = request.cursor
        if request.section_id or (cursor and cursor.handle_digest != snapshot):
            return ReaderIssue(status="invalid_address", message="Invalid abstract section or cursor.")
        content = record.abstract
        citation = request.citation
        start = citation.locator.start if citation else (cursor.offset if cursor else 0)
        stop = citation.locator.end if citation else len(content)
        if start >= stop or stop > len(content):
            return ReaderIssue(status="invalid_address", message="Range extends outside abstract.")
        # UTF-8 bytes bound content tokens conservatively for byte-based tokenizers.
        # Bound before encoding. Dropping an incomplete trailing code point is lossless
        # across pages because the cursor counts characters actually emitted.
        prefix = content[start:min(stop, start + self.max_chars, start + request.max_tokens)]
        fragment = prefix.encode("utf-8")[:request.max_tokens].decode("utf-8", errors="ignore")
        end = start + len(fragment)
        if not fragment or (citation and end != stop):
            return ReaderIssue(status="budget_too_small", message="Budget cannot fit the requested content.")
        locator = AbstractLocator(field=JsonPointerLocator(kind="json_pointer", value="/abstract"),
                                  start=start, end=end)
        block = ReaderBlock(content=fragment, locator=locator,
                            block_id=_digest([snapshot, start, end]))
        if citation and block.block_id != citation.block_id:
            return ReaderIssue(status="invalid_address", message="Citation does not match source and range.")
        next_cursor = ReaderCursor(handle_digest=snapshot, offset=end) if end < len(content) and not citation else None
        return RecordReadResult(blocks=[block], next_cursor=next_cursor)
