"""Record-level access to immutable parsed materials and their source maps."""
from hashlib import sha256
import re

from ..material_store import MaterialStore
from ..schemas.failures import FailureCode
from ..schemas.materials import MaterialOpenRequest, MaterialRef, canonical_material_json
from ..schemas.reader import (
    IndexedFullTextHandle, LocatedObject, FullTextHandle, FullTextLocator, OpenedRecord, ReaderBlock, ReaderCursor, ReaderIssue,
    RecordReadResult, SectionEntry,
)
from ..schemas.references import ArtifactRole
from .material_reader import LocalMaterialReader
from .object_content import read_object
from .reader_objects import DetectionUnavailable, indexed_handle, reference_page


def _key(*values):
    return sha256(canonical_material_json(values)).hexdigest()


def _binding(raw, record):
    return next((b for b in raw.bindings if b.source_record_id == record.source_record_id
                 and b.work_id == record.work_id and b.external_id == record.external_id
                 and b.selector.kind == "document"), None)


def _catalog(manifest, record):
    return {a.artifact_id: a for a in manifest.artifacts if a.role == ArtifactRole.FULL_TEXT
            and a.work_id == record.work_id and a.source_record_id == record.source_record_id}


def _registered(raw, record, catalog):
    entry = catalog.get(raw.ref.artifact_id)
    binding = _binding(raw, record)
    pinned = re.search(r"v[0-9]+$", record.external_id or "") if record.source_id == "arxiv" else None
    return (entry is not None and binding is not None and raw.role == "full_text"
            and (not pinned or binding.observed_version == pinned.group())
            and raw.provenance.source_id == record.source_id
            and (entry.sha256, entry.relative_path, entry.version_label, entry.media_type, entry.byte_size)
            == (raw.ref.sha256, raw.relative_path, binding.observed_version, raw.format.media_type, raw.byte_size))


def _handle(raw, parsed, binding):
    return FullTextHandle(
        subject_paper_id=raw.ref.subject_paper_id, namespace=raw.ref.namespace,
        adapter="registered_full_text_v1", source_record_id=binding.source_record_id,
        work_id=binding.work_id, external_id=binding.external_id, observed_version=binding.observed_version,
        source_format=raw.format.family, raw_id=raw.ref.artifact_id, sha256=raw.ref.sha256,
        parsed_id=parsed.ref.artifact_id, parsed_sha256=parsed.ref.sha256, map_sha256=parsed.source_map.sha256,
    )


class FullTextReader:
    max_output_chars = 4_000_000

    def __init__(self, references):
        self.store = MaterialStore(references)

    def text_selections(self, scope, manifest, record_ids):
        """Map authorized original IDs to the text artifact that open will produce."""
        materials = self.store.load(scope)
        records = {r.source_record_id: r for r in manifest.source_records if r.source_record_id in record_ids}
        catalog = {a.artifact_id: a for a in manifest.artifacts if a.role == ArtifactRole.FULL_TEXT}
        selections = {}
        for raw in materials.raw:
            for binding in raw.bindings:
                record = records.get(binding.source_record_id)
                if record is None or not _registered(raw, record, catalog):
                    continue
                parser = LocalMaterialReader.parser_for(raw, self.max_output_chars)
                if parser is None:
                    continue
                # Historical cached outputs used a content-dependent ID.
                cached = next((p for p in materials.parsed if p.source_ref == raw.ref
                               and p.binding_sha256 == binding.binding_sha256 and p.parser == parser), None)
                selections[raw.ref.artifact_id] = (cached.ref.artifact_id if cached else
                                                   LocalMaterialReader.parsed_id(raw, binding, parser))
        return selections

    def open(self, scope, manifest, record, requested_format, *, raw_id=None):
        catalog = _catalog(manifest, record)
        formats = [requested_format] if requested_format else ["html", "pdf"]
        candidates = []
        for raw in self.store.load(scope).raw:
            if raw_id is not None and raw.ref.artifact_id != raw_id:
                continue
            if raw.format.family not in formats or not _binding(raw, record):
                continue
            if raw.ref.artifact_id not in catalog:
                continue
            if not _registered(raw, record, catalog):
                return ReaderIssue(status="integrity_error", message="Raw descriptor disagrees with record catalog.")
            candidates.append(raw)
        if not candidates:
            legacy = any(a.source_record_id == record.source_record_id and a.role == ArtifactRole.EXTRACTED_TEXT
                         for a in manifest.artifacts)
            return ReaderIssue(status="unsupported_format" if catalog or legacy else "unavailable",
                               message="No registered full text in a supported requested format.")
        # Prefer newest acquisition within a format. Keep fallbacks on the same observed version.
        candidates.sort(key=lambda item: item.provenance.acquired_at, reverse=True)
        version = _binding(candidates[0], record).observed_version
        candidates.sort(key=lambda item: formats.index(item.format.family))
        warnings = []
        for raw in candidates:
            binding = _binding(raw, record)
            if binding.observed_version != version:
                continue
            result = LocalMaterialReader(self.store).open_sync(MaterialOpenRequest(
                schema_version=1, request_id="reader-open", run_id="offline-reader", raw_ref=raw.ref,
                source_record_id=record.source_record_id, binding_sha256=binding.binding_sha256,
                max_output_chars=self.max_output_chars,
            ))
            if any(issue.code == FailureCode.READER_VERIFICATION_FAILED for issue in result.issues):
                return ReaderIssue(status="integrity_error", message="Registered full text or cache failed verification.")
            if result.document is None:
                warnings.append(f"{raw.format.family.value}: {result.status}")
                continue
            parsed, text, mapping = self.store.parsed(result.document.ref)
            warnings.extend(parsed.warnings)
            outline = []
            for i, entry in enumerate(mapping.entries):
                if entry.locator.kind == "pdf_page":
                    label = f"Page {entry.locator.page}"
                elif entry.locator.kind == "html_xpath" and "/section" in entry.locator.value:
                    label = text[entry.parsed_char_start:min(entry.parsed_char_end, entry.parsed_char_start+160)].strip()
                    if not label:
                        continue
                    label = label.splitlines()[0][:100]
                else:
                    continue
                if len(outline) == 100:
                    warnings.append("Outline limited to 100 entries; the whole text remains pageable.")
                    break
                outline.append(SectionEntry(section_id=str(i), heading_path=[label]))
            handle = _handle(raw, parsed, binding)
            try:
                handle = indexed_handle(self.store, raw, handle)
            except DetectionUnavailable as exc:
                warnings.append(f"Object locations unavailable ({exc}).")
            return OpenedRecord(handle=handle, source_format=raw.format.family,
                                outline=outline, warnings=_bounded_warnings(warnings))
        return ReaderIssue(status="parse_failed", message="Full text could not be opened.", warnings=_bounded_warnings(warnings))

    def read(self, manifest, record, request, max_chars):
        handle = request.handle
        scope = {"subject_paper_id": handle.subject_paper_id, "namespace": handle.namespace}
        parsed_ref = MaterialRef(**scope, artifact_id=handle.parsed_id, sha256=handle.parsed_sha256)
        # Look up the pinned identities before opening bytes. Never accept a path from the caller.
        registry = self.store.load(parsed_ref)
        raw = next((r for r in registry.raw if r.ref.artifact_id == handle.raw_id), None)
        parsed = next((p for p in registry.parsed if p.ref == parsed_ref), None)
        if (not raw or not parsed or not _registered(raw, record, _catalog(manifest, record))
                or parsed.source_ref != raw.ref or parsed.source_record_id != record.source_record_id):
            return ReaderIssue(status="stale_handle", message="Full-text binding has changed.")
        binding = _binding(raw, record)
        expected = _handle(raw, parsed, binding)
        if (parsed.binding_sha256 != binding.binding_sha256
                or expected.model_dump() != handle.model_dump(exclude={"object_index_sha256"})):
            return ReaderIssue(status="stale_handle", message="Parsed snapshot does not match the handle.")
        _, text, mapping = self.store.parsed(parsed_ref)
        index = self.store.reader_objects(handle, expected) if isinstance(handle, IndexedFullTextHandle) else None
        if request.object_cursor:
            cursor = request.object_cursor
            page = reference_page(index, handle, text, mapping.entries, cursor.start, cursor.end, cursor)
            return page or ReaderIssue(status="invalid_address", message="Invalid object reference cursor.")
        if request.object_id:
            obj = next((o for o in index.objects if o.object_id == request.object_id), None)
            if obj is None:
                return ReaderIssue(status="invalid_address", message="Object does not belong to this snapshot.")
            return read_object(self.store, raw, handle, obj, min(max_chars, request.max_tokens))
        result = _page(handle, request, text, mapping.entries, max_chars)
        if index and isinstance(result, RecordReadResult):
            page = reference_page(index, handle, text, mapping.entries,
                                  result.blocks[0].locator.start, result.blocks[-1].locator.end)
            result.object_refs = page.object_refs
            result.next_object_cursor = page.next_object_cursor
        return result


def _bounded_warnings(warnings):
    if len(warnings) <= 32:
        return warnings
    return warnings[:31] + [f"{len(warnings) - 31} additional notices omitted."]


def _page(handle, request, text, entries, max_chars):
    invalid = ReaderIssue(status="invalid_address", message="Invalid section, cursor or citation for this document.")
    section = request.section_id
    if section is not None:
        # Use exact identifiers issued by open (zero-based source-map entries).
        matches = [e for i, e in enumerate(entries) if str(i) == section]
        if not matches:
            return invalid
        entries = matches
    low, high = entries[0].parsed_char_start, entries[-1].parsed_char_end
    snapshot = _key(handle.model_dump(mode="json"))
    cursor_key = _key(snapshot, section)
    cursor, citation = request.cursor, request.citation
    if cursor and cursor.handle_digest != cursor_key:
        return invalid
    if citation and not isinstance(citation.locator, FullTextLocator):
        return invalid
    start = citation.locator.start if citation else cursor.offset if cursor else low
    stop = citation.locator.end if citation else high
    if not low <= start < stop <= high:
        return invalid
    bounded = text[start:min(stop, start+max_chars, start+request.max_tokens)]
    fragment = bounded.encode()[:request.max_tokens].decode(errors="ignore")
    end = start + len(fragment)
    if start == end or (citation and end != stop):
        return ReaderIssue(status="budget_too_small", message="Read budget cannot fit the requested text.")
    blocks = []
    for entry in entries:
        a, b = max(start, entry.parsed_char_start), min(end, entry.parsed_char_end)
        if a >= b:
            continue
        locator = FullTextLocator(origin=entry.locator, start=a, end=b)
        blocks.append(ReaderBlock(kind="full_text", locator=locator, content=text[a:b],
                                  block_id=_key(snapshot, locator.model_dump(mode="json"))))
        if len(blocks) == 16:
            # Each block carries a citation. Bound that overhead independently of text.
            end = b
            break
    if citation and (len(blocks) != 1 or blocks[0].block_id != citation.block_id or blocks[0].locator != citation.locator):
        return invalid
    next_cursor = ReaderCursor(handle_digest=cursor_key, offset=end) if not citation and end < high else None
    return RecordReadResult(blocks=blocks, next_cursor=next_cursor)
