"""论文查新工作流的最低数据契约。"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .failures import FailureEvent, RecoveryDecision


class StrictModel(BaseModel):
    """拒绝未声明字段，避免 Agent 静默改变接口。"""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class PaperInput(StrictModel):
    """查新工作流的论文输入。"""

    paper_id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    abstract: str = ""
    english_abstract: str = ""
    full_text: str = Field(min_length=1)
    references: list[str] = Field(default_factory=list)
    claimed_contributions: list[str] = Field(default_factory=list)
    keywords_zh: list[str] = Field(default_factory=list)
    keywords_en: list[str] = Field(default_factory=list)
    metadata: dict[str, str] = Field(default_factory=dict)
    images: list[PaperImage] = Field(default_factory=list)
    tables: list[PaperTable] = Field(default_factory=list)
    equations: list[PaperEquation] = Field(default_factory=list)


class PaperPage(StrictModel):
    """论文处理模块的单页文本，page 从 1 开始。"""

    page: int = Field(ge=1)
    text: str = ""


class PaperImage(StrictModel):
    """MinerU 提取的图片/图表/印章等视觉块。"""

    image_id: str = ""
    kind: str = "image"  # image | chart | seal
    page: int = Field(ge=1)
    path: str = ""
    caption: str = ""
    footnote: str = ""
    bbox: list[float] = Field(default_factory=list)
    raw: dict[str, Any] = Field(default_factory=dict)


class PaperTable(StrictModel):
    """MinerU 提取的表格结构化块。"""

    table_id: str = ""
    page: int = Field(ge=1)
    caption: str = ""
    footnote: str = ""
    body: str = ""
    body_format: str = "html"
    bbox: list[float] = Field(default_factory=list)
    raw: dict[str, Any] = Field(default_factory=dict)


class PaperEquation(StrictModel):
    """MinerU 提取的公式块（LaTeX 表示）。"""

    equation_id: str = ""
    page: int = Field(ge=1)
    latex: str = ""
    bbox: list[float] = Field(default_factory=list)
    raw: dict[str, Any] = Field(default_factory=dict)


class PaperDocument(StrictModel):
    """论文处理模块的结构化产物（PDF → 文本 → 章节切分）。"""

    paper_id: str = Field(min_length=1)
    title: str = ""
    abstract: str = ""
    english_abstract: str = ""
    full_text: str = ""
    references: list[str] = Field(default_factory=list)
    claimed_contributions: list[str] = Field(default_factory=list)
    keywords_zh: list[str] = Field(default_factory=list)
    keywords_en: list[str] = Field(default_factory=list)
    metadata: dict[str, str] = Field(default_factory=dict)
    sections: dict[str, str] = Field(default_factory=dict)
    pages: list[PaperPage] = Field(default_factory=list)
    source: str = ""
    parse_warnings: list[str] = Field(default_factory=list)
    images: list[PaperImage] = Field(default_factory=list)
    tables: list[PaperTable] = Field(default_factory=list)
    equations: list[PaperEquation] = Field(default_factory=list)


class PaperReadingReference(StrictModel):
    """Frozen input index identity; paths inside the workspace are relative."""

    workspace: str = Field(min_length=1)
    index_path: str = Field(min_length=1)
    paper_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class PaperDigest(StrictModel):
    """供查新点提取的精简论文摘要视图。"""

    paper_id: str = Field(min_length=1)
    title: str = ""
    abstract: str = ""
    english_abstract: str = ""
    claimed_contributions: list[str] = Field(default_factory=list)
    keywords_zh: list[str] = Field(default_factory=list)
    keywords_en: list[str] = Field(default_factory=list)
    references: list[str] = Field(default_factory=list)
    full_text_excerpt: str = ""
    paper_source: PaperReadingReference | None = None


class NoveltyPoint(StrictModel):
    """可检索、可比较的单个查新点。"""

    point_id: str = Field(min_length=1)
    claim: str = Field(min_length=1)
    claim_en: str = ""
    technical_features: list[str] = Field(default_factory=list)
    technical_features_en: list[str] = Field(default_factory=list)
    source_locations: list[str] = Field(default_factory=list)


class ResearchTask(StrictModel):
    """某个 NoveltyPoint 下的一条独立调研任务。"""

    task_id: str = Field(min_length=1)
    novelty_point_id: str = Field(min_length=1)
    task_type: str = Field(min_length=1)
    language: str = Field(min_length=1)
    description: str = ""
    attempt: int = Field(default=1, ge=1)


class SearchConcept(StrictModel):
    """检索中的一个语义概念及其词项表达。

    role/alias/exclude/importance 为 v2 可选字段（默认 None/空），旧调用方不传时
    行为与 v1 完全一致；M2 模板编译器会填充这些字段。
    """

    concept_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    terms: list[str] = Field(min_length=1)
    role: Literal["object", "method", "feature", "setting", "escape"] | None = None
    alias: list[str] = Field(default_factory=list)
    exclude: list[str] = Field(default_factory=list)
    importance: int = Field(default=2, ge=1, le=3)


class SearchStrategy(StrictModel):
    """一条数据库无关的检索策略。

    use_alias 为 v2 可选字段（默认 False）：strict 只渲染 terms，medium/broad
    渲染 terms+alias，由编译器模板赋值；旧调用方不传时行为与 v1 一致。
    """

    strategy_id: str = Field(min_length=1)
    level: str = Field(min_length=1)
    expression: str = Field(min_length=1)
    description: str = ""
    use_alias: bool = False
    use_exclude: bool = True


class SearchPlan(StrictModel):
    """ResearchTask 对应的数据库无关检索计划。"""

    task_id: str = Field(min_length=1)
    novelty_point_id: str = Field(min_length=1)
    concepts: list[SearchConcept] = Field(min_length=1)
    strategies: list[SearchStrategy] = Field(min_length=1)
    protected_concept_ids: list[str] = Field(default_factory=list)


class NoveltyBrief(StrictModel):
    """Coordinator 在第一阶段产生的查新规划。"""

    paper_summary: str = Field(min_length=1)
    research_problem: str = ""
    novelty_points: list[NoveltyPoint] = Field(min_length=1)
    keywords_zh: list[str] = Field(default_factory=list)
    keywords_en: list[str] = Field(default_factory=list)
    research_tasks: list[ResearchTask] = Field(default_factory=list)


class EvidenceSource(StrictModel):
    """Evidence Card 中可追溯的原始文献证据。"""

    title: str = Field(min_length=1)
    quote: str | None = None
    location: str | None = None
    doi: str | None = None
    url: str | None = None


class EvidenceCard(StrictModel):
    """文献调研 Agent 对候选文献的结构化分析。"""

    card_id: str = Field(min_length=1)
    task_id: str = Field(min_length=1)
    novelty_point_id: str = Field(min_length=1)
    document_title: str = Field(min_length=1)
    main_contribution: str = Field(min_length=1)
    overlaps: list[str] = Field(default_factory=list)
    differences: list[str] = Field(default_factory=list)
    sources: list[EvidenceSource] = Field(default_factory=list)
    cited_by_paper: bool | None = None
    possible_baseline: bool = False
    relevance: float = Field(ge=0.0, le=1.0)
    confidence: float = Field(ge=0.0, le=1.0)
    evidence_ids: list[str] = Field(default_factory=list)


class RejectedEvidence(StrictModel):
    """未通过框架证据门槛的 Evidence Card。"""

    card_id: str
    reason: str


class IssueSeverity(StrEnum):
    WARNING = "warning"
    ERROR = "error"


class WorkflowIssue(StrictModel):
    """可恢复错误或质量问题的审计记录。"""

    node: str
    code: str
    message: str
    severity: IssueSeverity = IssueSeverity.WARNING
    task_id: str | None = None


class ReviewVerdict(StrEnum):
    """证据 Reviewer 对单张 Evidence Card 的裁定。"""

    ACCEPT = "accept"
    REJECT = "reject"
    NEEDS_MORE_EVIDENCE = "needs_more_evidence"


class EvidenceReviewIssue(StrictModel):
    """Reviewer 对单张证据卡发现的具体问题。"""

    code: str
    message: str
    severity: IssueSeverity = IssueSeverity.WARNING
    field: str | None = None
    source_index: int | None = None


class EvidenceReviewDecision(StrictModel):
    """Reviewer 对单张 Evidence Card 的结构化决定。"""

    card_id: str
    verdict: ReviewVerdict
    issues: list[EvidenceReviewIssue] = Field(default_factory=list)
    reviewed_confidence: float = Field(ge=0.0, le=1.0)


class ReviewStatus(StrEnum):
    """查新点级 Reviewer 的完成状态。"""

    REVIEWED = "reviewed"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"


class NoveltyVerdict(StrEnum):
    """Reviewer 对单个查新点给出的新颖性评价。"""

    NOVEL = "novel"
    PARTIALLY_NOVEL = "partially_novel"
    NOT_NOVEL = "not_novel"


class RelevantWork(StrictModel):
    """对查新点结论高度相关、且仅以可信句柄引用的 Work。"""

    work_id: str = Field(min_length=1)
    card_ids: list[str] = Field(min_length=1)
    evidence_ids: list[str] = Field(min_length=1)
    relevance_reason: str = Field(min_length=1)


class SupplementRequest(StrictModel):
    """Reviewer 的补检语义建议；V0 不拥有工作流路由权。"""

    reason: str = Field(min_length=1)
    missing_aspects: list[str] = Field(default_factory=list)
    suggested_focus: list[str] = Field(default_factory=list)


class ReviewReadCitation(StrictModel):
    """Model-selected read; the harness resolves its exact quote and evidence ID."""

    read_id: str = Field(min_length=1)
    char_start: int | None = Field(default=None, ge=0)
    char_end: int | None = Field(default=None, ge=0)


class ReviewEvidence(StrictModel):
    # Exact slice coordinates require retaining leading and trailing whitespace.
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)

    evidence_id: str = Field(min_length=1)
    review_id: str = Field(min_length=1)
    origin_card_id: str = Field(min_length=1)
    novelty_point_id: str = Field(min_length=1)
    work_id: str = Field(min_length=1)
    source_record_id: str | None = None
    artifact_id: str = Field(min_length=1)
    namespace: str = Field(min_length=1)
    artifact_hash: str = Field(min_length=1)
    read_id: str = Field(min_length=1)
    char_start: int = Field(ge=0)
    char_end: int = Field(ge=0)
    exact_quote: str = Field(min_length=1)
    role: str = Field(min_length=1)
    content_extent: str = Field(min_length=1)
    version_label: str | None = None


class FeatureComparison(StrictModel):
    feature_id: str = Field(min_length=1)
    work_id: str = Field(min_length=1)
    relation: Literal["supported", "partially_supported", "contradicted", "unknown"]
    basis_type: Literal["direct_statement", "semantic_equivalence", "grounded_inference", "insufficient"]
    evidence_refs: list[str] = Field(default_factory=list)
    reason: str = Field(min_length=1)
    source_context: Literal["author_method", "related_work", "baseline", "ablation", "unknown"] = "unknown"


class ReviewerReadAudit(StrictModel):
    """Harness-owned metadata for one successfully executed Reviewer read."""
    read_id: str
    namespace: str
    work_id: str
    artifact_id: str
    artifact_hash: str
    char_start: int = Field(ge=0)
    char_end: int = Field(ge=0)
    text_sha256: str


class NoveltyPointReview(StrictModel):
    """一个 NoveltyPoint 的结构化信息判定结果。"""

    novelty_point_id: str = Field(min_length=1)
    status: ReviewStatus
    verdict: NoveltyVerdict | None = None
    verdict_reason: str | None = None
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    highly_relevant_works: list[RelevantWork] = Field(default_factory=list)
    supplement_request: SupplementRequest | None = None
    read_citations: list[ReviewReadCitation] = Field(default_factory=list)
    review_evidence: list[ReviewEvidence] = Field(default_factory=list)
    feature_comparisons: list[FeatureComparison] = Field(default_factory=list)
    incomplete_reason: Literal["semantic_evidence", "material_unavailable", "budget_exhausted", "technical_error"] | None = None
    execution_issues: list[FailureEvent] = Field(default_factory=list)
    reader_observations: list[ReviewerReadAudit] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_review_semantics(self) -> "NoveltyPointReview":
        if self.status is ReviewStatus.REVIEWED:
            if self.verdict is None:
                raise ValueError("reviewed result requires verdict")
            if not self.verdict_reason:
                raise ValueError("reviewed result requires verdict_reason")
            if self.confidence is None:
                raise ValueError("reviewed result requires confidence")
        elif self.verdict is not None:
            raise ValueError("insufficient_evidence result cannot have verdict")
        return self


class ReviewerCardDraft(StrictModel):
    """Model-owned single-card judgment; provenance is registered by the harness."""

    novelty_point_id: str = Field(min_length=1)
    status: ReviewStatus
    verdict: NoveltyVerdict | None = None
    verdict_reason: str | None = None
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    highly_relevant_works: list[RelevantWork] = Field(default_factory=list)
    supplement_request: SupplementRequest | None = None
    read_citations: list[ReviewReadCitation] = Field(default_factory=list)
    feature_comparisons: list[FeatureComparison] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_review_semantics(self) -> "ReviewerCardDraft":
        if self.status is ReviewStatus.REVIEWED:
            if self.verdict is None or not self.verdict_reason or self.confidence is None:
                raise ValueError("reviewed result requires verdict, verdict_reason and confidence")
        elif self.verdict is not None:
            raise ValueError("insufficient_evidence result cannot have verdict")
        return self


class ReviewerSummaryDraft(StrictModel):
    """Model-owned point synthesis; it cannot introduce reads or evidence objects."""

    novelty_point_id: str = Field(min_length=1)
    status: ReviewStatus
    verdict: NoveltyVerdict | None = None
    verdict_reason: str | None = None
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    highly_relevant_works: list[RelevantWork] = Field(default_factory=list)
    supplement_request: SupplementRequest | None = None
    feature_comparisons: list[FeatureComparison] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_review_semantics(self) -> "ReviewerSummaryDraft":
        if self.status is ReviewStatus.REVIEWED:
            if self.verdict is None or not self.verdict_reason or self.confidence is None:
                raise ValueError("reviewed result requires verdict, verdict_reason and confidence")
        elif self.verdict is not None:
            raise ValueError("insufficient_evidence result cannot have verdict")
        return self


class NoveltyConclusion(StrictModel):
    """单个查新点的最终结论；新颖性字段直接继承 Reviewer。"""

    novelty_point_id: str = Field(min_length=1)
    review_status: ReviewStatus
    verdict: NoveltyVerdict | None = None
    verdict_reason: str | None = None
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    summary: str
    supporting_card_ids: list[str] = Field(default_factory=list)
    counter_card_ids: list[str] = Field(default_factory=list)
    highly_relevant_works: list[RelevantWork] = Field(default_factory=list)
    review_evidence: list[ReviewEvidence] = Field(default_factory=list)
    feature_comparisons: list[FeatureComparison] = Field(default_factory=list)
    incomplete_reason: Literal["semantic_evidence", "material_unavailable", "budget_exhausted", "technical_error"] | None = None

    @model_validator(mode="after")
    def validate_review_semantics(self) -> "NoveltyConclusion":
        if self.review_status is ReviewStatus.REVIEWED:
            if self.verdict is None or not self.verdict_reason:
                raise ValueError("reviewed conclusion requires verdict and verdict_reason")
            if self.confidence is None:
                raise ValueError("reviewed conclusion requires confidence")
        elif self.verdict is not None:
            raise ValueError("insufficient_evidence conclusion cannot have verdict")
        return self


class ConclusionNarrativeDraft(StrictModel):
    """Model-owned prose and card grouping, without Reviewer authority fields."""

    novelty_point_id: str = Field(min_length=1)
    summary: str = Field(min_length=1, max_length=600)
    supporting_card_ids: list[str] = Field(default_factory=list, max_length=24)
    counter_card_ids: list[str] = Field(default_factory=list, max_length=24)


class ReportNarrativeDraft(StrictModel):
    """The complete model output contract for report synthesis."""

    conclusions: list[ConclusionNarrativeDraft] = Field(min_length=1, max_length=32)
    limitations: list[Annotated[str, Field(min_length=1, max_length=400)]] = Field(
        default_factory=list, max_length=12
    )


class PartialCardReview(StrictModel):
    """An original card result whose point-level summary has not completed."""
    card_id: str = Field(min_length=1)
    novelty_point_id: str = Field(min_length=1)
    review: NoveltyPointReview
    summary_status: Literal["failed", "incomplete"]

    @model_validator(mode="after")
    def same_point(self):
        if self.review.novelty_point_id != self.novelty_point_id:
            raise ValueError("partial card review must retain its point binding")
        return self


class NoveltyReport(StrictModel):
    """查新工作流输出给后续系统的报告。"""

    partial_card_reviews: list[PartialCardReview] = Field(default_factory=list)
    execution_failures: list[FailureEvent] = Field(default_factory=list)
    recovery_decisions: list[RecoveryDecision] = Field(default_factory=list)
    point_coverage: dict = Field(default_factory=dict)
    point_lifecycle: list[dict] = Field(default_factory=list)
    paper_id: str
    conclusions: list[NoveltyConclusion] = Field(default_factory=list)
    missing_references: list[str] = Field(default_factory=list)
    missing_baselines: list[str] = Field(default_factory=list)
    citation_issues: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)


class InsufficientFinalEvidence(StrictModel):
    """A point whose final valid EvidenceCard count is below the system cut."""

    novelty_point_id: str = Field(min_length=1)
    valid_card_count: int = Field(ge=0)
    required_card_count: int = Field(ge=1)
    reason: Literal["insufficient_final_evidence"] = "insufficient_final_evidence"


class NoveltyRunResult(StrictModel):
    """一次完整工作流的可测试结果。"""

    failure_events: list[FailureEvent] = Field(default_factory=list)
    recovery_decisions: list[RecoveryDecision] = Field(default_factory=list)
    brief: NoveltyBrief
    evidence_cards: list[EvidenceCard]
    rejected_evidence: list[RejectedEvidence]
    novelty_reviews: list[NoveltyPointReview] = Field(default_factory=list)
    insufficient_final_evidence_points: list[InsufficientFinalEvidence]
    issues: list[WorkflowIssue]
    rounds: int
    report: NoveltyReport
