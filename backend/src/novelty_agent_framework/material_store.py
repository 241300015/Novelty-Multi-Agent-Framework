"""Immutable material files and their versioned, per-namespace registry.

Files are published before the registry. Interrupted writes can leave unregistered
files, never a successfully registered partial file. A subsequent identical write
can adopt them; automatic deletion is deliberately excluded from acquisition.
"""
from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path

from .persistence import (
    ReferenceStore, _MANIFEST_WRITE_LOCK, _atomic_write_json, _safe_storage_id, paper_workspace,
    artifact_namespace_for_reference_namespace,
)
from .schemas.materials import (
    MaterialManifest, MaterialRef, MaterialScope, ParsedMaterialArtifact,
    RawMaterialArtifact, SourceMap,
)
from .schemas import Artifact, ArtifactRole


def digest(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


class MaterialStore:
    def __init__(self, reference_store: ReferenceStore | None = None):
        self.references = reference_store or ReferenceStore()

    def _root(self, scope: MaterialScope) -> Path:
        if scope.namespace != artifact_namespace_for_reference_namespace(self.references.namespace):
            raise ValueError("material namespace does not match injected store")
        _safe_storage_id(scope.subject_paper_id, "subject_paper_id")
        workspace = paper_workspace(scope.subject_paper_id, output_root=self.references.output_root, create=False)
        if not workspace.resolve().is_relative_to(self.references.output_root.resolve()):
            raise ValueError("paper workspace escapes output root")
        directory = "references" if scope.namespace.value == "research_reference" else "subject_references"
        root = workspace / directory
        if not root.resolve().is_relative_to(self.references.output_root.resolve()):
            raise ValueError("material scope escapes output root")
        root.mkdir(parents=True, exist_ok=True)
        return root.resolve()

    def _path(self, scope: MaterialScope, relative_path: str) -> Path:
        root = self._root(scope)
        path = (root / relative_path).resolve()
        if not path.is_relative_to(root):
            raise ValueError("material path escapes namespace root")
        return path

    def load(self, scope: MaterialScope) -> MaterialManifest:
        path = self._path(scope, "materials.json")
        if not path.exists():
            return MaterialManifest(schema_version=1, scope=scope, raw=[], parsed=[])
        manifest = MaterialManifest.model_validate_json(path.read_bytes())
        if manifest.scope != MaterialScope.model_validate(scope.model_dump(include={"subject_paper_id", "namespace"})):
            raise ValueError("material registry scope mismatch")
        return manifest

    def _write(self, scope: MaterialScope, path: str, body: bytes) -> None:
        target = self._path(scope, path)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            if target.read_bytes() != body:
                raise ValueError("immutable material file already contains different bytes")
            return
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as handle:
                temporary = Path(handle.name)
                handle.write(body)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, target)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def _bytes(self, scope: MaterialScope, path: str, sha256: str, size: int) -> bytes:
        body = self._path(scope, path).read_bytes()
        if digest(body) != sha256 or len(body) != size:
            raise ValueError("material hash or byte size mismatch")
        return body

    @staticmethod
    def scope(ref: MaterialRef) -> MaterialScope:
        return MaterialScope(subject_paper_id=ref.subject_paper_id, namespace=ref.namespace)

    def raw(self, ref: MaterialRef) -> tuple[RawMaterialArtifact, bytes]:
        item = next((a for a in self.load(self.scope(ref)).raw if a.ref == ref), None)
        if item is None:
            raise ValueError("raw reference is not registered with this hash")
        return item, self._bytes(ref, item.relative_path, ref.sha256, item.byte_size)

    def parsed(self, ref: MaterialRef) -> tuple[ParsedMaterialArtifact, str, SourceMap]:
        item = next((a for a in self.load(self.scope(ref)).parsed if a.ref == ref), None)
        if item is None:
            raise ValueError("parsed reference is not registered with this hash")
        raw, _ = self.raw(item.source_ref)
        binding = next((b for b in raw.bindings if b.source_record_id == item.source_record_id
                        and b.binding_sha256 == item.binding_sha256 and b.work_id == item.work_id), None)
        if binding is None:
            raise ValueError("parsed binding is not registered on the raw source")
        body = self._bytes(ref, item.relative_path, ref.sha256, item.byte_size)
        text = body.decode("utf-8")
        index = item.source_map
        mapping = SourceMap.model_validate_json(self._bytes(ref, index.relative_path, index.sha256, index.byte_size))
        self._validate_parsed(item, text, mapping, raw)
        return item, text, mapping

    @staticmethod
    def _validate_parsed(item, text, mapping, raw):
        if len(text) != item.char_count or mapping.parsed_ref != item.ref or mapping.source_ref != item.source_ref:
            raise ValueError("parsed text or source-map identity mismatch")
        if raw.content_extent.value == "partial" and item.content_extent.value != "partial":
            raise ValueError("parsed extent cannot upgrade a partial source")
        if raw.content_extent.value == "unknown" and item.content_extent.value == "full":
            raise ValueError("parsed extent cannot upgrade unknown coverage")
        end = 0
        for entry in sorted(mapping.entries, key=lambda e: e.parsed_char_start):
            if entry.parsed_char_start > end or entry.parsed_char_end > len(text):
                raise ValueError("source-map has a gap or exceeds parsed text")
            end = max(end, entry.parsed_char_end)
        if end != len(text):
            raise ValueError("source-map does not cover parsed text")

    def save_raw(self, item: RawMaterialArtifact, body: bytes) -> RawMaterialArtifact:
        if digest(body) != item.ref.sha256 or len(body) != item.byte_size:
            raise ValueError("raw body does not match descriptor")
        scope = self.scope(item.ref)
        with _MANIFEST_WRITE_LOCK:
            manifest = self.load(scope)
            existing = next((a for a in manifest.raw if a.ref.artifact_id == item.ref.artifact_id), None)
            if existing:
                # New acquisition provenance does not overwrite historical origin.
                if (existing.ref != item.ref or existing.bindings != item.bindings
                        or existing.role != item.role or existing.format != item.format
                        or existing.relative_path != item.relative_path):
                    raise ValueError("raw identity conflicts with registered material")
                self.raw(existing.ref)
                return existing
            updated = MaterialManifest(schema_version=1, scope=scope,
                                       raw=[*manifest.raw, item], parsed=manifest.parsed)
            self._write(scope, item.relative_path, body)
            _atomic_write_json(self._path(scope, "materials.json"), updated.model_dump(mode="json"))
        return item

    def save_parsed(self, item: ParsedMaterialArtifact, text: str, mapping: SourceMap) -> ParsedMaterialArtifact:
        from .schemas.materials import canonical_material_json
        body = text.encode("utf-8")
        index_body = canonical_material_json(mapping.model_dump(mode="json")) + b"\n"
        if (digest(body) != item.ref.sha256 or len(body) != item.byte_size
                or digest(index_body) != item.source_map.sha256 or len(index_body) != item.source_map.byte_size):
            raise ValueError("parsed bytes do not match descriptor")
        raw, _ = self.raw(item.source_ref)
        self._validate_parsed(item, text, mapping, raw)
        scope = self.scope(item.ref)
        with _MANIFEST_WRITE_LOCK:
            manifest = self.load(scope)
            existing = next((a for a in manifest.parsed if a.ref.artifact_id == item.ref.artifact_id), None)
            if existing:
                old = existing.model_dump(exclude={"created_at"})
                new = item.model_dump(exclude={"created_at"})
                if old != new:
                    raise ValueError("parsed identity conflicts with registered material")
                self.parsed(existing.ref)
                return existing
            updated = MaterialManifest(schema_version=1, scope=scope, raw=manifest.raw,
                                       parsed=[*manifest.parsed, item])
            self._write(scope, item.relative_path, body)
            self._write(scope, item.source_map.relative_path, index_body)
            _atomic_write_json(self._path(scope, "materials.json"), updated.model_dump(mode="json"))
        return item

    def save_object_png(self, scope, body):
        checksum = digest(body)
        self._write(scope, f"media/{checksum}.png", body)
        return checksum

    def object_png(self, scope, image):
        return self._bytes(scope, f"media/{image.sha256}.png", image.sha256, image.byte_size)

    def _asset_binding(self, raw, src):
        key = digest(src.encode("utf-8"))
        return f"assets/{raw.ref.sha256}/{key}.json"

    def save_html_asset(self, raw, src, url, body):
        from .schemas.materials import canonical_material_json
        checksum = digest(body)
        self._write(raw.ref, f"assets/blobs/{checksum}", body)
        descriptor = {"source_sha256":raw.ref.sha256, "src":src, "url":url,
                      "sha256":checksum, "byte_size":len(body)}
        self._write(raw.ref, self._asset_binding(raw, src), canonical_material_json(descriptor))

    def html_asset(self, raw, src):
        import json
        import re
        path = self._path(raw.ref, self._asset_binding(raw, src))
        if not path.exists():
            return None
        descriptor = json.loads(path.read_bytes())
        checksum = descriptor["sha256"]
        if (not re.fullmatch(r"[0-9a-f]{64}", checksum) or descriptor["src"] != src
                or descriptor["source_sha256"] != raw.ref.sha256):
            raise ValueError("HTML asset binding mismatch")
        return self._bytes(raw.ref, f"assets/blobs/{checksum}", checksum, descriptor["byte_size"])

    def save_reader_objects(self, index):
        from .schemas.materials import canonical_material_json

        body = canonical_material_json(index.model_dump(mode="json"))
        checksum = digest(body)
        work = _safe_storage_id(index.source.work_id, "work_id")
        self._write(index.source, f"documents/{work}/objects/{checksum}.json", body)
        return checksum

    def reader_objects(self, handle, expected_source):
        from .schemas.reader import ReaderObjectIndex

        work = _safe_storage_id(handle.work_id, "work_id")
        checksum = handle.object_index_sha256
        body = self._path(handle, f"documents/{work}/objects/{checksum}.json").read_bytes()
        if digest(body) != checksum:
            raise ValueError("object index integrity check failed")
        index = ReaderObjectIndex.model_validate_json(body)
        if index.source != expected_source:
            raise ValueError("object index belongs to another full-text snapshot")
        return index

    def raw_catalog_artifact(self, ref: MaterialRef, source_record_id: str) -> Artifact:
        """Register a verified raw file in list.json without parsing or copying it."""
        raw, _ = self.raw(ref)
        binding = next((b for b in raw.bindings if b.source_record_id == source_record_id), None)
        if binding is None:
            raise ValueError("source record has no binding on the raw material")
        if len(raw.bindings) != 1:
            raise ValueError("legacy catalog cannot represent a multi-record raw response")
        return Artifact(
            artifact_id=raw.ref.artifact_id, work_id=binding.work_id,
            source_record_id=binding.source_record_id, role=ArtifactRole(raw.role.value),
            media_type=raw.format.media_type, relative_path=raw.relative_path,
            sha256=raw.ref.sha256, byte_size=raw.byte_size,
            content_extent=raw.content_extent, version_label=binding.observed_version,
            acquired_at=raw.provenance.acquired_at,
            provenance={"material_representation": "raw", "material_ref": raw.ref.model_dump(mode="json")},
        )

    def legacy_artifacts(self, parsed: ParsedMaterialArtifact) -> tuple[Artifact, Artifact]:
        """Project verified v1 files into the existing evidence catalog without copying bytes."""
        parsed, _, _ = self.parsed(parsed.ref)
        raw, _ = self.raw(parsed.source_ref)
        binding = next(b for b in raw.bindings if b.binding_sha256 == parsed.binding_sha256)
        raw_artifact = Artifact(
            artifact_id=raw.ref.artifact_id, work_id=binding.work_id,
            source_record_id=binding.source_record_id, role=ArtifactRole.FULL_TEXT,
            media_type=raw.format.media_type, relative_path=raw.relative_path,
            sha256=raw.ref.sha256, byte_size=raw.byte_size,
            content_extent=raw.content_extent, version_label=binding.observed_version,
            acquired_at=raw.provenance.acquired_at,
            provenance={"material_representation": "raw", "material_ref": raw.ref.model_dump(mode="json")},
        )
        parsed_artifact = Artifact(
            artifact_id=parsed.ref.artifact_id, work_id=parsed.work_id,
            source_record_id=parsed.source_record_id, role=ArtifactRole.EXTRACTED_TEXT,
            media_type=parsed.media_type, relative_path=parsed.relative_path,
            sha256=parsed.ref.sha256, byte_size=parsed.byte_size,
            content_extent=parsed.content_extent, version_label=binding.observed_version,
            derived_from_artifact_id=raw.ref.artifact_id, acquired_at=parsed.created_at,
            extraction_warnings=parsed.warnings,
            provenance={"material_representation": "parsed", "material_ref": parsed.ref.model_dump(mode="json")},
        )
        return raw_artifact, parsed_artifact
