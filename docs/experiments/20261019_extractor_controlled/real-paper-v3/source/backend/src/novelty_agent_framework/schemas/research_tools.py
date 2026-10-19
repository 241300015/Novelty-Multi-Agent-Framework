"""Data contracts for concrete tools exposed to a Researcher agent."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, TypeAlias

from pydantic import ConfigDict, Field, StringConstraints, field_validator, model_validator

from .failures import FailureCode, FailureEvent
from .domain import EvidenceCard, StrictModel
from .references import (
    AccessStatus,
    ArtifactHandle,
    ArtifactNamespace,
    ArtifactRole,
    ContentExtent,
    Evidence,
)

NonEmptyStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class DatabaseSearchArguments(StrictModel):
    """Select one configured database; task and query scope stay runtime-owned."""

    source_id: NonEmptyStr
    full_text_source_record_ids: list[NonEmptyStr] = Field(default_factory=list, max_length=4)

    @field_validator("source_id")
    @classmethod
    def normalize_source_id(cls, value: str) -> str:
        return value.lower()


class DatabaseSearchItem(StrictModel):
    source_record_id: NonEmptyStr
    work_id: NonEmptyStr
    title: NonEmptyStr
    authors: list[NonEmptyStr] = Field(default_factory=list)
    publication_year: int | None = None
    source_id: NonEmptyStr
    access_status: AccessStatus
    artifact_ids: list[NonEmptyStr] = Field(default_factory=list)
    full_text_artifact_ids: list[NonEmptyStr] = Field(default_factory=list)
    abstract_preview: str | None = None


class DatabaseSearchResult(StrictModel):
    source_id: NonEmptyStr
    results: list[DatabaseSearchItem] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class WebSearchArguments(StrictModel):
    """Resource-discovery input; no result produced here is evidence."""

    query: NonEmptyStr
    max_results: int = Field(default=10, ge=1, le=100)


class WebSearchItem(StrictModel):
    source_record_id: NonEmptyStr
    rank: int = Field(ge=1)
    title: NonEmptyStr
    url: NonEmptyStr
    snippet: str | None = None
    score: float | None = None
    published_at: datetime | None = None
    source_name: NonEmptyStr | None = None


class WebSearchResult(StrictModel):
    query: NonEmptyStr
    results: list[WebSearchItem] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class BrowserArguments(StrictModel):
    """Fetch a known source reference; browsing does not judge evidence."""

    source_record_id: NonEmptyStr


class BrowserArtifactItem(StrictModel):
    artifact_id: NonEmptyStr
    role: ArtifactRole
    media_type: NonEmptyStr
    content_extent: ContentExtent


class BrowserResult(StrictModel):
    source_record_id: NonEmptyStr
    work_id: NonEmptyStr
    artifacts: list[BrowserArtifactItem] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class ReaderArguments(StrictModel):
    """Agent 可见的读取参数。

    这里**没有** namespace：artifact_id 由工具生成、模型只是转述，它没有可靠
    依据判断制品属于研究语料还是论文自带参考语料。实测把 namespace 交给模型
    （默认值 research_reference）会让 reference_search 召回的自带参考语料全部
    读失败，因此命名空间由 ReaderTool 按制品归属自动判定。
    """

    artifact_id: NonEmptyStr
    char_start: int = Field(default=0, ge=0)
    max_chars: int = Field(default=8_000, ge=1, le=16_000)


class ReaderCallArguments(ReaderArguments):
    """One legacy read or up to four independently addressed fragments."""

    artifact_id: NonEmptyStr | None = None
    reads: list[ReaderArguments] | None = Field(default=None, min_length=1, max_length=4)

    @model_validator(mode="after")
    def exclusive_read_mode(self):
        if (self.artifact_id is None) == (self.reads is None):
            raise ValueError("provide artifact_id or reads, exclusively")
        if self.reads is not None and self.model_fields_set & {"char_start", "max_chars"}:
            raise ValueError("batch offsets and limits belong inside each reads item")
        return self


class ReferenceReadRequest(StrictModel):
    """Internal deterministic-reader request derived from ReaderArguments."""

    subject_paper_id: NonEmptyStr
    namespace: ArtifactNamespace = ArtifactNamespace.RESEARCH_REFERENCE
    artifact_id: NonEmptyStr
    char_start: int = Field(default=0, ge=0)
    max_chars: int = Field(default=8_000, ge=1, le=16_000)


class ReferenceReadResult(StrictModel):
    """一次受限读取的结果。

    ``text`` 必须逐字保留：它既是引文的唯一来源，又由 ``char_start``/``char_end``
    精确描述区间。``StrictModel`` 默认 strip 字符串首尾空白，一旦切片以空白开头
    或结尾，"区间长度 == 正文长度" 就不成立——读取会直接失败，或者更糟：正文被
    静默改动而区间照旧。实测模型读到第 16000 字符之后必然报错，因此这里显式关闭。
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)

    namespace: ArtifactNamespace
    read_id: NonEmptyStr
    work_id: NonEmptyStr
    artifact_id: NonEmptyStr
    role: ArtifactRole
    char_start: int = Field(ge=0)
    char_end: int = Field(ge=0)
    text: str
    has_more: bool
    sha256: NonEmptyStr

    @model_validator(mode="after")
    def valid_range(self) -> ReferenceReadResult:
        if self.char_end < self.char_start:
            raise ValueError("char_end must not be before char_start")
        if self.char_end - self.char_start != len(self.text):
            raise ValueError("read range must match text length")
        return self


class ReferenceSearchArguments(StrictModel):
    query: NonEmptyStr
    max_results: int = Field(default=10, ge=1, le=100)


class ReferenceSearchItem(StrictModel):
    reference_id: NonEmptyStr
    work_id: NonEmptyStr
    title: NonEmptyStr
    authors: list[NonEmptyStr] = Field(default_factory=list)
    publication_year: int | None = None
    artifact_handles: list[ArtifactHandle] = Field(default_factory=list)
    abstract_preview: str | None = None
    score: float


class ReferenceSearchResult(StrictModel):
    query: NonEmptyStr
    results: list[ReferenceSearchItem] = Field(default_factory=list)


class EvidenceQuoteDraft(StrictModel):
    """A semantic quote selection without model-supplied provenance handles."""

    quote: NonEmptyStr
    interpretation: NonEmptyStr
    confidence: float = Field(ge=0.0, le=1.0)


class EvidenceCardDraft(StrictModel):
    """One model-authored semantic assessment to be bound by the runtime."""

    main_contribution: NonEmptyStr
    overlaps: list[NonEmptyStr] = Field(default_factory=list)
    differences: list[NonEmptyStr] = Field(default_factory=list)
    quotes: list[EvidenceQuoteDraft] = Field(min_length=1)
    possible_baseline: bool = False
    relevance: float = Field(ge=0.0, le=1.0)
    confidence: float = Field(ge=0.0, le=1.0)


class ResearchFinishDraft(StrictModel):
    cards: list[EvidenceCardDraft] = Field(default_factory=list)
    no_evidence_reason: NonEmptyStr | None = None

    @model_validator(mode="after")
    def require_cards_or_reason(self) -> ResearchFinishDraft:
        if self.cards and self.no_evidence_reason is not None:
            raise ValueError("no_evidence_reason must be absent when cards are present")
        if not self.cards and self.no_evidence_reason is None:
            raise ValueError("no_evidence_reason is required when cards are empty")
        return self


class EvidenceCardBuilderRequest(StrictModel):
    draft: ResearchFinishDraft


class EvidenceCardBuildRejection(StrictModel):
    code: FailureCode = FailureCode.MODEL_OUTPUT_REFERENCE
    node: str = "builder.compile"
    card_index: int = Field(ge=0)
    reason: NonEmptyStr


class EvidenceCardBuilderResult(StrictModel):
    evidence: list[Evidence] = Field(default_factory=list)
    evidence_cards: list[EvidenceCard] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    rejections: list[EvidenceCardBuildRejection] = Field(default_factory=list)
    correction_rejections: list[EvidenceCardBuildRejection] = Field(default_factory=list)
    correction_failure: FailureEvent | None = None


# Deprecated name retained for import compatibility during schema migration.
ReferenceReaderToolArguments: TypeAlias = ReaderArguments
