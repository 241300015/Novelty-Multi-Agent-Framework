"""Provider/Reader v1 wire contracts for immutable raw and parsed materials.

Validation here is local to each DTO. The runtime must additionally verify the
request/result relationship, registered bindings, file bytes/hashes, source-map
coverage and paths after resolving symlinks. These models perform no I/O.
"""

from __future__ import annotations

import hashlib
import json
import re
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import (
    AfterValidator,
    AwareDatetime,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from .domain import StrictModel
from .failures import FailureEvent
from .references import ArtifactNamespace, ContentExtent


NonEmptyStr = Annotated[str, StringConstraints(min_length=1, pattern=r"\S")]
Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
PositiveInt = Annotated[int, Field(strict=True, gt=0)]
NonNegativeInt = Annotated[int, Field(strict=True, ge=0)]
HttpStatus = Annotated[int, Field(strict=True, ge=100, le=599)]
DocumentRole = Literal["metadata", "abstract", "full_text", "supplement"]
ParsedMediaType = Literal["text/plain", "text/markdown"]


def canonical_material_json(value: Any) -> bytes:
    """Canonical bytes for binding/config/read identity, never for raw bodies."""
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def _safe_relative_path(value: str) -> str:
    # A portable POSIX relative path; filesystem containment is a runtime check.
    if (
        value != value.strip()
        or "\\" in value
        or ":" in value
        or any(ord(char) < 32 for char in value)
        or any(part in {"", ".", ".."} for part in value.split("/"))
    ):
        raise ValueError("relative_path must be a safe POSIX relative file path")
    return value


RelativePath = Annotated[NonEmptyStr, AfterValidator(_safe_relative_path)]


class MaterialModel(StrictModel):
    # Source pointers, digest inputs and returned text must never be trimmed.
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)


class VersionedMaterialModel(MaterialModel):
    schema_version: Literal[1]

    @field_validator("schema_version", mode="before")
    @classmethod
    def validate_version_type(cls, value: Any) -> Any:
        # Python equality otherwise lets True / 1.0 satisfy Literal[1].
        if type(value) is not int:
            raise ValueError("schema_version must be an integer")
        return value


class MaterialRole(StrEnum):
    SEARCH_RESPONSE = "search_response"
    METADATA = "metadata"
    ABSTRACT = "abstract"
    FULL_TEXT = "full_text"
    SUPPLEMENT = "supplement"


class FormatFamily(StrEnum):
    LATEX = "latex"
    HTML = "html"
    PDF = "pdf"
    XML = "xml"
    JSON = "json"
    TEXT = "text"


class MaterialScope(MaterialModel):
    subject_paper_id: NonEmptyStr
    namespace: ArtifactNamespace


class MaterialRef(MaterialScope):
    artifact_id: NonEmptyStr
    sha256: Sha256


def _validate_derivation(parsed: MaterialRef, source: MaterialRef) -> None:
    if (parsed.subject_paper_id, parsed.namespace) != (
        source.subject_paper_id, source.namespace
    ):
        raise ValueError("parsed and source references must share paper and namespace")
    if parsed.artifact_id == source.artifact_id:
        raise ValueError("parsed representation must have an independent artifact_id")


class DocumentTarget(MaterialModel):
    work_id: NonEmptyStr
    source_record_id: NonEmptyStr
    external_id: NonEmptyStr
    requested_version: NonEmptyStr | None


class DocumentLocator(MaterialModel):
    kind: Literal["document"]


class JsonPointerLocator(MaterialModel):
    kind: Literal["json_pointer"]
    value: str

    @model_validator(mode="after")
    def validate_pointer(self) -> JsonPointerLocator:
        # Empty string denotes the entire document under RFC 6901.
        if (self.value and not self.value.startswith("/")) or re.search(
            r"~(?![01])", self.value
        ):
            raise ValueError("value must be an RFC 6901 JSON pointer")
        return self


class XmlXPathLocator(MaterialModel):
    kind: Literal["xml_xpath"]
    value: NonEmptyStr
    namespaces: dict[NonEmptyStr, NonEmptyStr]


class HtmlXPathLocator(MaterialModel):
    kind: Literal["html_xpath"]
    value: NonEmptyStr


RecordSelector = Annotated[
    DocumentLocator | JsonPointerLocator | XmlXPathLocator | HtmlXPathLocator,
    Field(discriminator="kind"),
]


class TextCharsLocator(MaterialModel):
    kind: Literal["text_chars"]
    start: NonNegativeInt
    end: PositiveInt
    charset: NonEmptyStr

    @model_validator(mode="after")
    def validate_range(self) -> TextCharsLocator:
        if self.end <= self.start:
            raise ValueError("source character range must be nonempty")
        return self


class PdfPageLocator(MaterialModel):
    kind: Literal["pdf_page"]
    page: PositiveInt


SourceLocator = Annotated[
    DocumentLocator | JsonPointerLocator | XmlXPathLocator | HtmlXPathLocator
    | TextCharsLocator | PdfPageLocator,
    Field(discriminator="kind"),
]


class RawRecordBinding(MaterialModel):
    work_id: NonEmptyStr
    source_record_id: NonEmptyStr
    external_id: NonEmptyStr
    observed_version: NonEmptyStr | None
    selector: RecordSelector
    binding_sha256: Sha256

    @model_validator(mode="after")
    def validate_digest(self) -> RawRecordBinding:
        payload = self.model_dump(mode="json", exclude={"binding_sha256"})
        expected = hashlib.sha256(canonical_material_json(payload)).hexdigest()
        if self.binding_sha256 != expected:
            raise ValueError("binding_sha256 does not match the binding fields")
        return self


class MaterialFormat(MaterialModel):
    family: FormatFamily
    media_type: Annotated[str, StringConstraints(pattern=r"^[a-z0-9!#$&^_.+\-]+/[a-z0-9!#$&^_.+\-]+$")]
    declared_content_type: str | None
    charset: NonEmptyStr | None
    schema_hint: NonEmptyStr | None

    @model_validator(mode="after")
    def validate_format(self) -> MaterialFormat:
        if self.family == FormatFamily.PDF:
            if self.charset is not None or self.media_type != "application/pdf":
                raise ValueError("PDF requires application/pdf and null charset")
        # XML/JSON vendor subtypes and schema hints remain extensible. The
        # provider validates bytes against family; the reader verifies structure.
        return self


class AcquisitionProvenance(MaterialModel):
    source_id: NonEmptyStr
    run_id: NonEmptyStr
    request_id: NonEmptyStr
    attempt_id: NonEmptyStr
    operation: NonEmptyStr
    view: NonEmptyStr | None
    request_url: NonEmptyStr
    final_url: NonEmptyStr
    http_status: HttpStatus
    acquired_at: AwareDatetime
    content_encoding: NonEmptyStr | None
    etag: str | None
    last_modified: str | None


class RawMaterialArtifact(VersionedMaterialModel):
    ref: MaterialRef
    representation: Literal["raw"]
    role: MaterialRole
    format: MaterialFormat
    relative_path: RelativePath
    byte_size: PositiveInt
    transfer_complete: Literal[True]
    content_extent: ContentExtent
    provenance: AcquisitionProvenance
    bindings: list[RawRecordBinding]
    warnings: list[str]

    @field_validator("transfer_complete", mode="before")
    @classmethod
    def validate_transfer_type(cls, value: Any) -> Any:
        if value is not True:
            raise ValueError("transfer_complete must be the boolean true")
        return value

    @model_validator(mode="after")
    def validate_raw(self) -> RawMaterialArtifact:
        ids = [binding.source_record_id for binding in self.bindings]
        if len(ids) != len(set(ids)):
            raise ValueError("bindings must have unique source_record_id values")
        status = self.provenance.http_status
        if not 200 <= status < 300 or status == 206:
            raise ValueError("raw material requires a successful non-range HTTP response")
        return self


class MaterialAcquisitionRequest(VersionedMaterialModel):
    request_id: NonEmptyStr
    run_id: NonEmptyStr
    scope: MaterialScope
    source_id: NonEmptyStr
    target: DocumentTarget
    requested_role: DocumentRole
    format_preferences: list[FormatFamily] = Field(min_length=1)
    allow_abstract_fallback: bool = Field(default=False, strict=True)
    max_bytes: PositiveInt

    @model_validator(mode="after")
    def validate_preferences(self) -> MaterialAcquisitionRequest:
        if len(self.format_preferences) != len(set(self.format_preferences)):
            raise ValueError("format_preferences must be unique and ordered")
        return self


class AcquisitionAttempt(MaterialModel):
    attempt_id: NonEmptyStr
    requested_format: FormatFamily
    status: Literal["acquired", "unavailable", "not_found", "unauthorized", "unsupported", "failed"]
    http_status: HttpStatus | None
    artifact_refs: list[MaterialRef]
    issues: list[FailureEvent]

    @model_validator(mode="after")
    def validate_status(self) -> AcquisitionAttempt:
        if self.status != "acquired" and self.artifact_refs:
            raise ValueError("unsuccessful attempts cannot return artifact_refs")
        if self.status == "failed" and not self.issues:
            raise ValueError("failed attempt requires issues")
        return self


class MaterialAcquisitionResult(VersionedMaterialModel):
    request_id: NonEmptyStr
    status: Literal["acquired", "partial", "not_found", "unavailable", "unauthorized", "unsupported", "failed"]
    materials: list[RawMaterialArtifact]
    attempts: list[AcquisitionAttempt]
    issues: list[FailureEvent]

    @model_validator(mode="after")
    def validate_status(self) -> MaterialAcquisitionResult:
        if bool(self.materials) != (self.status in {"acquired", "partial"}):
            raise ValueError("only acquired/partial results require and allow materials")
        if self.status == "failed" and not self.issues:
            raise ValueError("failed acquisition requires issues")
        ids = [attempt.attempt_id for attempt in self.attempts]
        if len(ids) != len(set(ids)):
            raise ValueError("attempt_id must be unique within a result")
        keys = [(m.ref.subject_paper_id, m.ref.namespace, m.ref.artifact_id) for m in self.materials]
        if len(keys) != len(set(keys)):
            raise ValueError("materials must have unique scoped artifact identities")
        return self


class MaterialOpenRequest(VersionedMaterialModel):
    request_id: NonEmptyStr
    run_id: NonEmptyStr
    raw_ref: MaterialRef
    source_record_id: NonEmptyStr
    binding_sha256: Sha256
    max_output_chars: PositiveInt


class ParserIdentity(MaterialModel):
    name: NonEmptyStr
    version: NonEmptyStr
    config_sha256: Sha256


class IndexFileRef(VersionedMaterialModel):
    relative_path: RelativePath
    sha256: Sha256
    byte_size: PositiveInt


class ParsedMaterialArtifact(VersionedMaterialModel):
    representation: Literal["parsed"]
    ref: MaterialRef
    source_ref: MaterialRef
    work_id: NonEmptyStr
    source_record_id: NonEmptyStr
    binding_sha256: Sha256
    role: DocumentRole
    parser: ParserIdentity
    media_type: ParsedMediaType
    relative_path: RelativePath
    byte_size: PositiveInt
    char_count: PositiveInt
    content_extent: ContentExtent
    source_map: IndexFileRef
    created_at: AwareDatetime
    warnings: list[str]

    @model_validator(mode="after")
    def validate_parsed(self) -> ParsedMaterialArtifact:
        _validate_derivation(self.ref, self.source_ref)
        if self.source_map.relative_path == self.relative_path:
            raise ValueError("source map and parsed text must use different files")
        if not self.char_count <= self.byte_size <= self.char_count * 4:
            raise ValueError("byte_size is inconsistent with UTF-8 char_count")
        return self


class MaterialOpenResult(VersionedMaterialModel):
    request_id: NonEmptyStr
    status: Literal["ready", "partial", "unsupported", "failed"]
    document: ParsedMaterialArtifact | None
    issues: list[FailureEvent]

    @model_validator(mode="after")
    def validate_status(self) -> MaterialOpenResult:
        if (self.document is not None) != (self.status in {"ready", "partial"}):
            raise ValueError("only ready/partial results require and allow a document")
        if self.status == "failed" and not self.issues:
            raise ValueError("failed open requires issues")
        if self.document is not None:
            is_partial = self.document.content_extent == ContentExtent.PARTIAL
            if is_partial != (self.status == "partial"):
                raise ValueError("partial status and document content_extent must agree")
        return self


class SourceMapping(MaterialModel):
    parsed_char_start: NonNegativeInt
    parsed_char_end: PositiveInt
    precision: Literal["exact", "structural", "document"]
    locator: SourceLocator

    @model_validator(mode="after")
    def validate_mapping(self) -> SourceMapping:
        if self.parsed_char_end <= self.parsed_char_start:
            raise ValueError("mapping range must be nonempty")
        if (self.locator.kind == "document") != (self.precision == "document"):
            raise ValueError("document locator and document precision must agree")
        if self.locator.kind == "pdf_page" and self.precision != "structural":
            raise ValueError("PDF page mappings have structural precision in v1")
        if self.locator.kind == "text_chars" and self.precision == "exact":
            if self.locator.end - self.locator.start != self.parsed_char_end - self.parsed_char_start:
                raise ValueError("exact text mapping requires equal character spans")
        return self


class SourceMap(VersionedMaterialModel):
    parsed_ref: MaterialRef
    source_ref: MaterialRef
    entries: list[SourceMapping] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_refs(self) -> SourceMap:
        _validate_derivation(self.parsed_ref, self.source_ref)
        return self


class MaterialReadRequest(VersionedMaterialModel):
    request_id: NonEmptyStr
    run_id: NonEmptyStr
    parsed_ref: MaterialRef
    char_start: NonNegativeInt
    max_chars: Annotated[int, Field(strict=True, ge=1, le=16000)]


class MaterialReadSlice(MaterialModel):
    read_id: Annotated[str, StringConstraints(pattern=r"^read_[0-9a-f]{24}$")]
    parsed_ref: MaterialRef
    source_ref: MaterialRef
    source_record_id: NonEmptyStr
    work_id: NonEmptyStr
    role: DocumentRole
    media_type: ParsedMediaType
    char_start: NonNegativeInt
    char_end: NonNegativeInt
    text: str
    has_more: bool = Field(strict=True)
    origins: list[SourceMapping]

    @model_validator(mode="after")
    def validate_slice(self) -> MaterialReadSlice:
        _validate_derivation(self.parsed_ref, self.source_ref)
        if self.char_end - self.char_start != len(self.text):
            raise ValueError("read range must match the exact Unicode text length")
        if not self.text:
            if self.has_more or self.origins:
                raise ValueError("empty EOF slice cannot have has_more or origins")
        elif not self.origins:
            raise ValueError("nonempty text requires a source mapping")
        for origin in self.origins:
            if not self.char_start <= origin.parsed_char_start < origin.parsed_char_end <= self.char_end:
                raise ValueError("origins must be clipped to the read range")
        return self


class MaterialReadResult(VersionedMaterialModel):
    request_id: NonEmptyStr
    status: Literal["content", "eof", "failed"]
    read: MaterialReadSlice | None
    issues: list[FailureEvent]

    @model_validator(mode="after")
    def validate_status(self) -> MaterialReadResult:
        if self.status == "failed":
            if self.read is not None or not self.issues:
                raise ValueError("failed read requires null read and nonempty issues")
        elif self.read is None:
            raise ValueError("content/eof requires a read slice")
        elif bool(self.read.text) != (self.status == "content"):
            raise ValueError("content requires nonempty text; eof requires empty text")
        return self


class MaterialManifest(VersionedMaterialModel):
    """Authoritative material registry for one paper and artifact namespace."""
    scope: MaterialScope
    raw: list[RawMaterialArtifact]
    parsed: list[ParsedMaterialArtifact]

    @model_validator(mode="after")
    def validate_registry(self) -> MaterialManifest:
        ids: set[str] = set()
        paths: set[str] = set()
        raw_by_id = {item.ref.artifact_id: item for item in self.raw}
        for item in [*self.raw, *self.parsed]:
            if (item.ref.subject_paper_id, item.ref.namespace) != (
                self.scope.subject_paper_id, self.scope.namespace
            ):
                raise ValueError("material belongs to another registry scope")
            if item.ref.artifact_id in ids:
                raise ValueError("artifact_id must be unique across raw and parsed")
            ids.add(item.ref.artifact_id)
            item_paths = [item.relative_path]
            if isinstance(item, ParsedMaterialArtifact):
                item_paths.append(item.source_map.relative_path)
                source = raw_by_id.get(item.source_ref.artifact_id)
                if source is None or source.ref != item.source_ref:
                    raise ValueError("parsed source_ref must match a registered raw material")
                if not any(b.source_record_id == item.source_record_id and b.work_id == item.work_id
                           and b.binding_sha256 == item.binding_sha256 for b in source.bindings):
                    raise ValueError("parsed binding must match a registered raw binding")
                if source.role != item.role:
                    raise ValueError("parsed role must preserve the raw document role")
            for path in item_paths:
                if path in paths:
                    raise ValueError("material file paths must be unique")
                paths.add(path)
        return self
