"""论文查新工作流的可替换接口。

这里刻意不绑定具体模型、学术数据库或全文解析库。实现方只需要实现这些
接口，就可以把真实能力接入 LangGraph 工作流。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Protocol, Sequence

from ..schemas import (
    EvidenceCard,
    EvidenceReviewDecision,
    EvidenceSource,
    InsufficientFinalEvidence,
    ExternalIdentifier,
    NoveltyBrief,
    NoveltyPoint,
    NoveltyPointReview,
    NoveltyPointReviewRequest,
    NoveltyReport,
    PaperDigest,
    PaperDocument,
    PaperInput,
    ParsedCitation,
    ResearchTask,
    SearchPlan,
    TaskResearchRequest,
    TaskResearchResult,
)


@dataclass(frozen=True)
class SearchHit:
    """搜索工具返回的最小候选文献描述。"""

    document_id: str
    title: str
    abstract: str = ""
    authors: tuple[str, ...] = ()
    year: int | None = None
    doi: str | None = None
    url: str | None = None
    source_id: str | None = None
    external_id: str | None = None
    full_text_url: str | None = None
    raw_metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class FullText:
    """全文工具返回的文本及其来源信息。"""

    document_id: str
    title: str
    text: str
    source: EvidenceSource | None = None
    sections: dict[str, str] = field(default_factory=dict)
    media_type: str = "text/plain"
    content_extent: str = "unknown"
    source_url: str | None = None
    version_label: str | None = None


class SearchTool(Protocol):
    """候选文献检索工具。"""

    def search(self, query: str, *, limit: int = 10) -> Sequence[SearchHit]:
        ...


class IdentifierResolver(Protocol):
    """Exact identifier resolution capability used outside agent tools."""

    source_id: str

    def resolve_identifier(self, identifier: ExternalIdentifier) -> SearchHit | None:
        ...


class KnownItemSearchTool(Protocol):
    """Citation/known-item retrieval, distinct from topical search."""

    source_id: str

    def search_known_item(
        self, citation: ParsedCitation, *, limit: int = 5
    ) -> Sequence[SearchHit]:
        ...


class FullTextTool(Protocol):
    """文献摘要或全文获取工具。"""

    def fetch(self, document_id: str) -> FullText | None:
        ...


class MetadataTool(Protocol):
    """文献元数据和引用信息核验工具。"""

    def resolve(self, document_id: str) -> EvidenceSource | None:
        ...


class NoveltyCoordinator(Protocol):
    """查新主 Agent 的能力边界。"""

    def plan(
        self,
        paper: PaperInput,
        *,
        points: Sequence[NoveltyPoint],
        attempt: int,
    ) -> NoveltyBrief:
        """把查新点转化为可并行执行的文献调研任务并组装查新规划。"""

    def plan_supplement(
        self,
        paper: PaperInput,
        *,
        brief: NoveltyBrief,
        existing_evidence: Sequence[EvidenceCard],
        insufficient_final_evidence_points: Sequence[InsufficientFinalEvidence],
        attempt: int,
    ) -> NoveltyBrief:
        """针对证据缺口生成补充调研任务。"""

    def synthesize(
        self,
        paper: PaperInput,
        *,
        brief: NoveltyBrief,
        evidence: Sequence[EvidenceCard],
        novelty_reviews: Sequence[NoveltyPointReview],
        rejected_evidence: Sequence[str],
        insufficient_final_evidence_points: Sequence[InsufficientFinalEvidence],
    ) -> NoveltyReport:
        """从全局视角汇总证据并形成查新报告。"""


class SearchPlanner(Protocol):
    """把查新点和调研任务转换为数据库无关的检索计划。"""

    async def aplan(
        self, point: NoveltyPoint, task: ResearchTask,
    ) -> SearchPlan:
        ...

    def plan(
        self,
        point: NoveltyPoint,
        task: ResearchTask,
    ) -> SearchPlan:
        ...


class LiteratureResearchAgent(Protocol):
    """对已经召回的候选文献进行查新点级证据分析。"""

    def research(
        self,
        task: ResearchTask,
        point: NoveltyPoint,
        candidates: Sequence[SearchHit],
        *,
        full_text_tool: FullTextTool | None = None,
        metadata_tool: MetadataTool | None = None,
    ) -> Sequence[EvidenceCard]:
        """逐篇分析给定候选文献并返回结构化文献证据。"""


class EvidenceValidator(Protocol):
    """证据质量门控接口。"""

    def validate(
        self,
        cards: Sequence[EvidenceCard],
        *,
        tasks: Sequence[ResearchTask],
    ) -> "ValidationResult":
        ...


@dataclass(frozen=True)
class ValidationResult:
    """校验器的标准输出。"""

    accepted: tuple[EvidenceCard, ...]
    rejected: tuple[tuple[str, str], ...]
    issues: tuple[tuple[str, str, str, str | None], ...] = ()


class EvidenceReviewer(Protocol):
    """综合当前查新点的 Cards/Evidence 并给出信息判定。"""

    def review(
        self,
        request: NoveltyPointReviewRequest,
    ) -> NoveltyPointReview | Awaitable[NoveltyPointReview]:
        ...


@dataclass(frozen=True)
class ReviewResult:
    """旧逐卡 Reviewer 输出，仅保留迁移期调用兼容。"""

    accepted: tuple[EvidenceCard, ...]
    rejected: tuple[tuple[str, str], ...]
    needs_more: tuple[str, ...]
    decisions: tuple[EvidenceReviewDecision, ...] = ()


class PaperProcessor(Protocol):
    """论文 PDF 处理能力：解析为结构化 PaperDocument，并提供工作流兼容视图。"""

    def process(
        self,
        source: str | Path,
        *,
        force_ocr: bool = False,
        paper_id: str | None = None,
    ) -> PaperDocument:
        """把 PDF 处理为结构化论文文档。"""

    def to_paper_input(self, document: PaperDocument) -> PaperInput:
        """把 PaperDocument 转换为工作流兼容的 PaperInput。"""


class NoveltyPointExtractor(Protocol):
    """查新点提取 Agent 的能力边界。"""

    def extract(
        self,
        digest: PaperDigest,
        *,
        previous_brief: NoveltyBrief | None,
        attempt: int,
    ) -> Sequence[NoveltyPoint]:
        """从论文摘要视图提取可检索、可比较的查新点。"""


class TaskResearcher(Protocol):
    """隔离执行单个 ResearchTask 的子工作流端口。"""

    async def ainvoke(self, request: TaskResearchRequest) -> TaskResearchResult:
        ...
