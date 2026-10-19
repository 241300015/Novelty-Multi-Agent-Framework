"""来源无关的参考文献、制品和原文证据数据契约。"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from math import isfinite
from pathlib import PurePosixPath
from typing import Annotated, Any

from pydantic import Field, StringConstraints, field_validator, model_validator

from .domain import NoveltyPoint, ResearchTask, SearchPlan, StrictModel
from .failures import FailureEvent

NonEmptyStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
JsonObject = dict[str, Any]


class WorkType(StrEnum):
    ARTICLE = "article"
    CONFERENCE_PAPER = "conference_paper"
    THESIS = "thesis"
    BOOK = "book"
    BOOK_CHAPTER = "book_chapter"
    PATENT = "patent"
    STANDARD = "standard"
    REPORT = "report"
    WEBPAGE = "webpage"
    DATASET = "dataset"
    SOFTWARE = "software"
    OTHER = "other"


class PublicationStage(StrEnum):
    """Persisted publication lifecycle metadata used by existing workspaces."""

    PREPRINT = "preprint"
    ACCEPTED_MANUSCRIPT = "accepted_manuscript"
    PUBLISHED = "published"
    RETRACTED = "retracted"
    UNKNOWN = "unknown"


class SourceKind(StrEnum):
    STRUCTURED_DATABASE = "structured_database"
    WEB = "web"  # Legacy manifests remain readable.
    WEB_SUPPLEMENT = "web_supplement"
    LOCAL = "local"
    USER_UPLOAD = "user_upload"


class AccessStatus(StrEnum):
    DISCOVERED = "discovered"
    METADATA_ONLY = "metadata_only"
    FULL_TEXT_AVAILABLE = "full_text_available"
    FULL_TEXT_ACQUIRED = "full_text_acquired"
    AUTH_REQUIRED = "auth_required"
    UNAVAILABLE = "unavailable"
    FAILED = "failed"


class ReferenceNamespace(StrEnum):
    RESEARCH = "research"
    SUBJECT_REFERENCE = "subject_reference"


class ArtifactNamespace(StrEnum):
    SUBJECT_REFERENCE = "subject_reference"
    RESEARCH_REFERENCE = "research_reference"


class ArtifactHandle(StrictModel):
    namespace: ArtifactNamespace
    artifact_id: NonEmptyStr


class ArtifactRole(StrEnum):
    ABSTRACT = "abstract"
    FULL_TEXT = "full_text"
    EXTRACTED_TEXT = "extracted_text"
    METADATA = "metadata"
    SUPPLEMENT = "supplement"


class ContentExtent(StrEnum):
    FULL = "full"
    PARTIAL = "partial"
    UNKNOWN = "unknown"


class SearchExecutionStatus(StrEnum):
    SUCCEEDED = "succeeded"
    PARTIAL = "partial"
    REQUIRES_HUMAN = "requires_human"
    FAILED = "failed"
    NOT_RUN = "not_run"


def _aware(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must include timezone information")
    return value


def _json_compatible(value: Any, path: str = "metadata") -> Any:
    if isinstance(value, float) and not isfinite(value):
        raise ValueError(f"{path} contains non-finite float")
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, list):
        for index, item in enumerate(value):
            _json_compatible(item, f"{path}[{index}]")
        return value
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError(f"{path} keys must be strings")
            _json_compatible(item, f"{path}.{key}")
        return value
    raise ValueError(f"{path} contains non-JSON-compatible value {type(value).__name__}")


def _unique_identifiers(identifiers: list[ExternalIdentifier]) -> list[ExternalIdentifier]:
    seen: set[tuple[str, str]] = set()
    for identifier in identifiers:
        key = (identifier.namespace, identifier.value)
        if key in seen:
            raise ValueError(f"duplicate external identifier {identifier.namespace}:{identifier.value}")
        seen.add(key)
    return identifiers


class ExternalIdentifier(StrictModel):
    namespace: NonEmptyStr
    value: NonEmptyStr

    @field_validator("namespace")
    @classmethod
    def normalize_namespace(cls, value: str) -> str:
        return value.lower()


class Work(StrictModel):
    work_id: NonEmptyStr
    work_type: WorkType
    title: NonEmptyStr
    authors: list[NonEmptyStr] = Field(default_factory=list)
    publication_year: int | None = None
    venue: NonEmptyStr | None = None
    language: NonEmptyStr | None = None
    identifiers: list[ExternalIdentifier] = Field(default_factory=list)
    canonical_source_record_id: NonEmptyStr | None = None
    publication_stage: PublicationStage = PublicationStage.UNKNOWN
    publication_stage_provenance: NonEmptyStr | None = None

    @field_validator("publication_year")
    @classmethod
    def valid_year(cls, value: int | None) -> int | None:
        if value is not None and not 1000 <= value <= datetime.now().year + 1:
            raise ValueError("publication_year is outside the supported range")
        return value

    @field_validator("identifiers")
    @classmethod
    def unique_identifiers(cls, value: list[ExternalIdentifier]) -> list[ExternalIdentifier]:
        return _unique_identifiers(value)


class SourceRecord(StrictModel):
    source_record_id: NonEmptyStr
    work_id: NonEmptyStr | None = None
    source_id: NonEmptyStr
    source_kind: SourceKind
    external_id: NonEmptyStr | None = None
    title: NonEmptyStr
    authors: list[NonEmptyStr] = Field(default_factory=list)
    abstract: NonEmptyStr | None = None
    publication_year: int | None = None
    identifiers: list[ExternalIdentifier] = Field(default_factory=list)
    landing_url: NonEmptyStr | None = None
    full_text_url: NonEmptyStr | None = None
    access_status: AccessStatus
    raw_metadata: JsonObject = Field(default_factory=dict)
    observed_at: datetime
    provenance: JsonObject = Field(default_factory=dict)
    publication_stage: PublicationStage = PublicationStage.UNKNOWN
    publication_stage_provenance: NonEmptyStr | None = None

    @field_validator("publication_year")
    @classmethod
    def valid_year(cls, value: int | None) -> int | None:
        return Work.valid_year(value)

    @field_validator("identifiers")
    @classmethod
    def unique_identifiers(cls, value: list[ExternalIdentifier]) -> list[ExternalIdentifier]:
        return _unique_identifiers(value)

    @field_validator("observed_at")
    @classmethod
    def aware_observed_at(cls, value: datetime) -> datetime:
        return _aware(value, "observed_at")

    @field_validator("raw_metadata", "provenance")
    @classmethod
    def json_metadata(cls, value: JsonObject, info: Any) -> JsonObject:
        return _json_compatible(value, info.field_name)


class Artifact(StrictModel):
    artifact_id: NonEmptyStr
    work_id: NonEmptyStr
    source_record_id: NonEmptyStr | None = None
    role: ArtifactRole
    media_type: NonEmptyStr
    relative_path: NonEmptyStr
    sha256: Annotated[str, StringConstraints(strip_whitespace=True, pattern=r"^[0-9a-fA-F]{64}$")]
    byte_size: int | None = Field(default=None, ge=0)
    language: NonEmptyStr | None = None
    version_label: NonEmptyStr | None = None
    content_extent: ContentExtent
    derived_from_artifact_id: NonEmptyStr | None = None
    acquired_at: datetime
    extraction_warnings: list[NonEmptyStr] = Field(default_factory=list)
    provenance: JsonObject = Field(default_factory=dict)

    @field_validator("relative_path")
    @classmethod
    def safe_relative_path(cls, value: str) -> str:
        if "\\" in value:
            raise ValueError("relative_path must use POSIX separators")
        path = PurePosixPath(value)
        if path.is_absolute() or not path.parts or path.parts[0] != "documents":
            raise ValueError("relative_path must be relative and located under documents/")
        if len(path.parts) < 3 or any(part in ("", ".", "..") for part in path.parts):
            raise ValueError("relative_path must identify a file under documents/<work_id>/")
        return path.as_posix()

    @field_validator("sha256")
    @classmethod
    def normalize_sha256(cls, value: str) -> str:
        return value.lower()

    @field_validator("acquired_at")
    @classmethod
    def aware_acquired_at(cls, value: datetime) -> datetime:
        return _aware(value, "acquired_at")

    @field_validator("provenance")
    @classmethod
    def json_metadata(cls, value: JsonObject) -> JsonObject:
        return _json_compatible(value, "provenance")

    @model_validator(mode="after")
    def validate_storage_name(self) -> Artifact:
        path = PurePosixPath(self.relative_path)
        if len(path.parts) != 3:
            raise ValueError(
                "relative_path must be documents/<work_id>/<artifact_id>.<extension>"
            )
        if path.parts[1] != self.work_id:
            raise ValueError("relative_path work directory must match work_id")
        if path.stem != self.artifact_id:
            raise ValueError("relative_path filename must use artifact_id")
        expected_extensions = {
            "application/pdf": {".pdf"},
            "application/json": {".json"},
            "text/html": {".html", ".htm"},
            "text/markdown": {".md", ".markdown"},
            "text/plain": {".txt"},
        }
        allowed = expected_extensions.get(self.media_type.lower())
        if allowed is not None and path.suffix.lower() not in allowed:
            raise ValueError(
                f"relative_path extension does not match media_type {self.media_type}"
            )
        return self


class EvidenceLocator(StrictModel):
    page_start: int | None = Field(default=None, ge=1)
    page_end: int | None = Field(default=None, ge=1)
    section: NonEmptyStr | None = None
    block_id: NonEmptyStr | None = None
    paragraph_start: int | None = Field(default=None, ge=1)
    paragraph_end: int | None = Field(default=None, ge=1)
    char_start: int | None = Field(default=None, ge=0)
    char_end: int | None = Field(default=None, ge=0)
    selector: NonEmptyStr | None = None

    @model_validator(mode="after")
    def validate_locator(self) -> EvidenceLocator:
        fields = (
            self.page_start, self.page_end, self.section, self.block_id,
            self.paragraph_start, self.paragraph_end, self.char_start,
            self.char_end, self.selector,
        )
        if all(value is None for value in fields):
            raise ValueError("locator must provide at least one location field")
        for name in ("page", "paragraph", "char"):
            start = getattr(self, f"{name}_start")
            end = getattr(self, f"{name}_end")
            if end is not None and start is None:
                raise ValueError(f"{name}_end requires {name}_start")
            if start is not None and end is not None and start > end:
                raise ValueError(f"{name}_start must not exceed {name}_end")
        return self


class Evidence(StrictModel):
    evidence_id: NonEmptyStr
    work_id: NonEmptyStr
    artifact_id: NonEmptyStr
    novelty_point_id: NonEmptyStr | None = None
    task_id: NonEmptyStr | None = None
    quote: NonEmptyStr
    locator: EvidenceLocator | None = None
    interpretation: NonEmptyStr
    confidence: float = Field(ge=0.0, le=1.0)
    provenance: JsonObject = Field(default_factory=dict)

    @field_validator("provenance")
    @classmethod
    def json_metadata(cls, value: JsonObject) -> JsonObject:
        return _json_compatible(value, "provenance")


class SearchResultRef(StrictModel):
    source_record_id: NonEmptyStr
    rank: int = Field(ge=1)
    score: float | None = None
    snippet: NonEmptyStr | None = None


class SearchExecution(StrictModel):
    execution_id: NonEmptyStr
    run_id: NonEmptyStr | None = None
    tool_name: NonEmptyStr
    source_id: NonEmptyStr
    query: NonEmptyStr
    parameters: JsonObject = Field(default_factory=dict)
    status: SearchExecutionStatus
    started_at: datetime
    completed_at: datetime | None = None
    results: list[SearchResultRef] = Field(default_factory=list)
    error: NonEmptyStr | None = None
    failure: FailureEvent | None = None

    @field_validator("started_at", "completed_at")
    @classmethod
    def aware_times(cls, value: datetime | None, info: Any) -> datetime | None:
        return None if value is None else _aware(value, info.field_name)

    @field_validator("parameters")
    @classmethod
    def json_parameters(cls, value: JsonObject) -> JsonObject:
        return _json_compatible(value, "parameters")

    @model_validator(mode="after")
    def validate_execution(self) -> SearchExecution:
        if self.completed_at is not None and self.completed_at < self.started_at:
            raise ValueError("completed_at must not be earlier than started_at")
        ranks = [result.rank for result in self.results]
        if len(ranks) != len(set(ranks)):
            raise ValueError("results contain duplicate rank")
        record_ids = [result.source_record_id for result in self.results]
        if len(record_ids) != len(set(record_ids)):
            raise ValueError("results contain duplicate source_record_id")
        return self


class ResearchBundle(StrictModel):
    execution_failures: list[FailureEvent] = Field(default_factory=list)
    bundle_id: NonEmptyStr
    producer: NonEmptyStr
    search_executions: list[SearchExecution] = Field(default_factory=list)
    works: list[Work] = Field(default_factory=list)
    source_records: list[SourceRecord] = Field(default_factory=list)
    artifacts: list[Artifact] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)
    warnings: list[NonEmptyStr] = Field(default_factory=list)


class StructuredSourceRetrievalRequest(StrictModel):
    """单个 ResearchTask 的结构化来源检索与读取请求。"""

    subject_paper_id: NonEmptyStr
    source_id: NonEmptyStr
    novelty_point: NoveltyPoint
    research_task: ResearchTask
    search_plan: SearchPlan
    run_id: NonEmptyStr | None = None

    @field_validator("source_id")
    @classmethod
    def normalize_source_id(cls, value: str) -> str:
        return value.lower()

    @model_validator(mode="after")
    def validate_task_identity(self) -> StructuredSourceRetrievalRequest:
        if self.research_task.novelty_point_id != self.novelty_point.point_id:
            raise ValueError(
                "research_task.novelty_point_id must match novelty_point.point_id"
            )
        if self.search_plan.task_id != self.research_task.task_id:
            raise ValueError("search_plan.task_id must match research_task.task_id")
        if self.search_plan.novelty_point_id != self.research_task.novelty_point_id:
            raise ValueError(
                "search_plan.novelty_point_id must match research_task.novelty_point_id"
            )
        return self


class ReferenceManifest(StrictModel):
    schema_version: NonEmptyStr = "1.0"
    subject_paper_id: NonEmptyStr
    updated_at: datetime
    works: list[Work] = Field(default_factory=list)
    source_records: list[SourceRecord] = Field(default_factory=list)
    artifacts: list[Artifact] = Field(default_factory=list)

    @field_validator("updated_at")
    @classmethod
    def aware_updated_at(cls, value: datetime) -> datetime:
        return _aware(value, "updated_at")

    @model_validator(mode="after")
    def validate_references(self) -> ReferenceManifest:
        works = _unique_by_id(self.works, "work_id")
        records = _unique_by_id(self.source_records, "source_record_id")
        artifacts = _unique_by_id(self.artifacts, "artifact_id")

        for work in self.works:
            canonical_id = work.canonical_source_record_id
            if canonical_id is not None:
                record = records.get(canonical_id)
                if record is None:
                    raise ValueError(f"work {work.work_id}.canonical_source_record_id references missing {canonical_id}")
                if record.work_id != work.work_id:
                    raise ValueError(f"work {work.work_id}.canonical_source_record_id {canonical_id} belongs to {record.work_id!r}")
        for record in self.source_records:
            if record.work_id is not None and record.work_id not in works:
                raise ValueError(f"source_record {record.source_record_id}.work_id references missing {record.work_id}")
        paths: set[str] = set()
        for artifact in self.artifacts:
            if artifact.work_id not in works:
                raise ValueError(f"artifact {artifact.artifact_id}.work_id references missing {artifact.work_id}")
            path_work_id = PurePosixPath(artifact.relative_path).parts[1]
            if path_work_id != artifact.work_id:
                raise ValueError(
                    f"artifact {artifact.artifact_id}.relative_path belongs to "
                    f"{path_work_id}, not work_id {artifact.work_id}"
                )
            if artifact.relative_path in paths:
                raise ValueError(f"artifact {artifact.artifact_id}.relative_path duplicates {artifact.relative_path}")
            paths.add(artifact.relative_path)
            if artifact.source_record_id is not None:
                record = records.get(artifact.source_record_id)
                if record is None:
                    raise ValueError(f"artifact {artifact.artifact_id}.source_record_id references missing {artifact.source_record_id}")
                if record.work_id is not None and record.work_id != artifact.work_id:
                    raise ValueError(f"artifact {artifact.artifact_id}.work_id conflicts with source_record {record.source_record_id}")
            parent_id = artifact.derived_from_artifact_id
            if parent_id == artifact.artifact_id:
                raise ValueError(f"artifact {artifact.artifact_id}.derived_from_artifact_id cannot self-reference")
            if parent_id is not None and parent_id not in artifacts:
                raise ValueError(f"artifact {artifact.artifact_id}.derived_from_artifact_id references missing {parent_id}")
        _reject_derivation_cycles(artifacts)
        artifact_record_ids = {item.source_record_id for item in self.artifacts}
        for record in self.source_records:
            if record.access_status == AccessStatus.FULL_TEXT_ACQUIRED and record.source_record_id not in artifact_record_ids:
                raise ValueError(f"source_record {record.source_record_id}.access_status is full_text_acquired but has no artifact")
        return self


def _unique_by_id(items: list[Any], field_name: str) -> dict[str, Any]:
    indexed: dict[str, Any] = {}
    for item in items:
        item_id = getattr(item, field_name)
        if item_id in indexed:
            raise ValueError(f"duplicate {field_name} {item_id}")
        indexed[item_id] = item
    return indexed


def _reject_derivation_cycles(artifacts: dict[str, Artifact]) -> None:
    for artifact_id in artifacts:
        chain: list[str] = []
        current: str | None = artifact_id
        while current is not None:
            if current in chain:
                cycle = " -> ".join([*chain[chain.index(current):], current])
                raise ValueError(f"derived_from_artifact_id cycle: {cycle}")
            chain.append(current)
            current = artifacts[current].derived_from_artifact_id
