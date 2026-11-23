"""Record-addressed Reader contracts. Legacy artifact reads keep their v1 DTOs."""
from typing import Annotated, Literal

from pydantic.json_schema import SkipJsonSchema
from pydantic import FiniteFloat, Field, model_serializer, model_validator

from .materials import (
    HtmlXPathLocator, PdfPageLocator, JsonPointerLocator, MaterialModel, MaterialScope, NonEmptyStr, NonNegativeInt,
    PositiveInt, Sha256, VersionedMaterialModel,
)
from .research_tools import ReaderCallArguments


class ReaderHandle(MaterialScope):
    """Identity of a specific version of the stored abstract field."""

    adapter: Literal["json_abstract_v1"] = "json_abstract_v1"
    source_record_id: NonEmptyStr
    work_id: NonEmptyStr | None
    external_id: NonEmptyStr | None
    sha256: Sha256


class FullTextHandle(MaterialScope):
    adapter: Literal["registered_full_text_v1"]
    source_record_id: NonEmptyStr
    work_id: NonEmptyStr
    external_id: NonEmptyStr
    source_format: Literal["html", "pdf"]
    observed_version: NonEmptyStr | None
    raw_id: NonEmptyStr
    sha256: Sha256
    parsed_id: NonEmptyStr
    parsed_sha256: Sha256
    map_sha256: Sha256


class IndexedFullTextHandle(FullTextHandle):
    """Pins the object detector's result as well as the original and parsed text."""
    object_index_sha256: Sha256


class HtmlObjectLocation(MaterialModel):
    kind: Literal["html_node"] = "html_node"
    xpath: NonEmptyStr
    element_id: NonEmptyStr | None = None


class PdfObjectLocation(MaterialModel):
    kind: Literal["pdf_region"] = "pdf_region"
    page: PositiveInt
    coordinate_space: Literal["unrotated_top_left_pt"] = "unrotated_top_left_pt"
    bbox: tuple[FiniteFloat, FiniteFloat, FiniteFloat, FiniteFloat]

    @model_validator(mode="after")
    def nonempty_region(self):
        x0, y0, x1, y1 = self.bbox
        if not (0 <= x0 < x1 and 0 <= y0 < y1):
            raise ValueError("expected a nonempty rectangle in page coordinates")
        return self


class ReaderObject(MaterialModel):
    object_id: Sha256
    kind: Literal["figure", "table"]
    label: NonEmptyStr | None = None
    locations: list[HtmlObjectLocation | PdfObjectLocation] = Field(min_length=1)


class ReaderObjectIndex(MaterialModel):
    schema_version: Literal[1] = 1
    source: FullTextHandle
    objects: list[ReaderObject]
    warnings: list[str] = Field(default_factory=list)


class ReaderMessage(VersionedMaterialModel):
    schema_version: Literal[1] = 1


class ReaderOpen(ReaderMessage):
    operation: Literal["open"]
    source_record_id: NonEmptyStr
    part: Literal["abstract", "full_text"]
    source_format: Literal["html", "pdf"] | None = None

    @model_validator(mode="after")
    def format_only_for_full_text(self):
        if self.source_format and self.part != "full_text":
            raise ValueError("source_format selects a full-text representation")
        return self


class ReaderCursor(MaterialModel):
    handle_digest: Sha256
    offset: PositiveInt


class AbstractLocator(MaterialModel):
    """Pointer is relative to the source record. Offsets count Unicode characters."""

    field: JsonPointerLocator
    start: NonNegativeInt
    end: PositiveInt

    @model_validator(mode="after")
    def check_range(self):
        if self.field.value != "/abstract" or self.start >= self.end:
            raise ValueError("expected a nonempty range within the abstract field")
        return self


class FullTextLocator(MaterialModel):
    """Unicode offsets in the pinned parsed text, plus a structural raw origin."""

    origin: HtmlXPathLocator | PdfPageLocator
    start: NonNegativeInt
    end: PositiveInt

    @model_validator(mode="after")
    def check_range(self):
        if self.end <= self.start:
            raise ValueError("empty full-text range")
        return self


class BlockReference(MaterialModel):
    locator: AbstractLocator | FullTextLocator
    block_id: Sha256


class ObjectRefsCursor(MaterialModel):
    """Continue the reference list for exactly one bounded text slice."""
    slice_digest: Sha256
    start: NonNegativeInt
    end: PositiveInt
    offset: PositiveInt


class ReaderRead(ReaderMessage):
    operation: Literal["read"]
    handle: IndexedFullTextHandle | FullTextHandle | ReaderHandle
    max_tokens: int = Field(default=2048, strict=True, ge=1, le=16000)
    section_id: NonEmptyStr | None = None
    cursor: ReaderCursor | None = None
    citation: BlockReference | None = None
    object_id: Sha256 | None = None
    object_cursor: ObjectRefsCursor | None = None

    @model_validator(mode="after")
    def check_address(self):
        if self.object_id and self.object_cursor:
            raise ValueError("choose object_id or object_cursor")
        if (self.object_id or self.object_cursor) and (
                not isinstance(self.handle, IndexedFullTextHandle) or self.section_id or self.cursor or self.citation):
            raise ValueError("object selectors require an indexed full-text handle and no other selector")
        if self.cursor and self.citation:
            raise ValueError("choose cursor or citation")
        return self


class CandidateContinuation(MaterialModel):
    handle: IndexedFullTextHandle | FullTextHandle | ReaderHandle
    cursor: ReaderCursor


class CandidatePage(MaterialModel):
    source_record_id: NonEmptyStr
    part: Literal["abstract", "full_text"]
    raw_id: SkipJsonSchema[NonEmptyStr | None] = None
    continuation: SkipJsonSchema[CandidateContinuation | None] = None


class RecordReaderCall(ReaderCallArguments):
    candidate: CandidatePage | None = None
    request: SkipJsonSchema[Annotated[ReaderOpen | ReaderRead, Field(discriminator="operation")] | None] = None

    @model_serializer(mode="wrap")
    def serialize_mode(self, handler):
        data = handler(self)
        if self.request is not None:
            return {"request": data["request"]}
        if self.candidate is not None:
            return {"candidate": data["candidate"], "max_chars": self.max_chars}
        data.pop("candidate", None)
        data.pop("request", None)
        return data

    @model_validator(mode="after")
    def exclusive_read_mode(self):
        if self.candidate is not None:
            if self.request or self.artifact_id is not None or self.reads is not None or "char_start" in self.model_fields_set:
                raise ValueError("candidate pages use an exclusive address")
            return self
        if self.request is None:
            return super().exclusive_read_mode()
        if self.model_fields_set & {"artifact_id", "reads", "char_start", "max_chars"}:
            raise ValueError("record and artifact arguments cannot be combined")
        return self


class SectionEntry(MaterialModel):
    section_id: NonEmptyStr
    heading_path: list[NonEmptyStr]


class ReaderResponse(ReaderMessage):
    warnings: list[str] = Field(default_factory=list)


class OpenedRecord(ReaderResponse):
    status: Literal["opened"] = "opened"
    handle: IndexedFullTextHandle | FullTextHandle | ReaderHandle
    source_format: Literal["json", "html", "pdf"] = "json"
    outline: list[SectionEntry] = Field(default_factory=list)


class ReaderBlock(BlockReference):
    kind: Literal["abstract", "full_text"] = "abstract"
    content_type: Literal["text/plain"] = "text/plain"
    content: str


class TableRow(MaterialModel):
    group: str | None = None
    cells: list[str]


class ObjectTable(MaterialModel):
    kind: Literal["table"] = "table"
    headers: list[list[str]]  # Each column carries its complete header path.
    rows: list[TableRow]


class ObjectImage(MaterialModel):
    kind: Literal["image"] = "image"
    media_type: Literal["image/png"] = "image/png"
    sha256: Sha256
    width: PositiveInt
    height: PositiveInt
    byte_size: PositiveInt


class LocatedObject(ReaderResponse):
    content: ObjectTable | ObjectImage | None = None
    status: Literal["located"] = "located"
    object: ReaderObject


class ObjectRefsPage(ReaderResponse):
    status: Literal["object_refs"] = "object_refs"
    object_refs: list[ReaderObject]
    next_object_cursor: ObjectRefsCursor | None = None


class RecordReadResult(ReaderResponse):
    next_object_cursor: ObjectRefsCursor | None = None
    object_refs: list[ReaderObject] = Field(default_factory=list)
    status: Literal["content"] = "content"
    blocks: list[ReaderBlock] = Field(min_length=1)
    next_cursor: ReaderCursor | None = None


class ReaderIssue(ReaderResponse):
    status: Literal["unavailable", "unsupported_format", "stale_handle",
                    "invalid_address", "budget_too_small", "parse_failed", "integrity_error"]
    message: NonEmptyStr
