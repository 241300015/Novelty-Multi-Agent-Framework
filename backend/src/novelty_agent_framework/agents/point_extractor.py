"""查新点提取：完整 digest 导览、持久化分段阅读和受控生成审核。"""
from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any

from backend.env import ModelCallOptions, ModelClient, ModelRegistry, PromptLibrary
from ..core.extractor_harness import ExtractorHarness, ExtractorHarnessConfig
from ..ports import NoveltyPointExtractor
from ..schemas import NoveltyBrief, NoveltyPoint, PaperDigest, PaperInput

DEMO_MAX_POINTS = 3
EXCERPT_CHAR_LIMIT = 2000
CONTRIBUTION_EXCERPT_LIMIT = 1200
TRUNCATION_MARK = "…[截断]"


def _truncate(text: str, limit: int) -> str:
    return text[:limit] + TRUNCATION_MARK if len(text) > limit else text


def build_paper_digest(paper: PaperInput) -> PaperDigest:
    """从 PaperInput 构造精简摘要视图（供查新点提取）。"""

    return PaperDigest(
        paper_id=paper.paper_id,
        title=paper.title,
        abstract=paper.abstract,
        english_abstract=paper.english_abstract,
        claimed_contributions=list(paper.claimed_contributions),
        keywords_zh=list(paper.keywords_zh),
        keywords_en=list(paper.keywords_en),
        references=[],  # 暂不向提取模型提供参考文献，避免引入噪声
        full_text_excerpt=_bounded_body_excerpt(paper.full_text),
    )


def _body_excerpt(full_text: str) -> str:
    """跳过封面页，从中文摘要标题之后开始截取正文片段。"""

    match = re.search(r"(?m)^\s*摘\s*要\s*$", full_text)
    return full_text[match.end():] if match else full_text


def _bounded_body_excerpt(full_text: str) -> str:
    """Add bounded author-contribution passages that may be beyond the opening."""
    opening = _truncate(_body_excerpt(full_text), EXCERPT_CHAR_LIMIT)
    passages = []
    for match in re.finditer(r"(?:主要|核心)贡献(?:可以)?总结", full_text):
        if match.start() < EXCERPT_CHAR_LIMIT:
            continue
        passage = full_text[match.start():match.start() + CONTRIBUTION_EXCERPT_LIMIT]
        if passage not in passages:
            passages.append(passage)
        if len(passages) == 3:
            break
    return opening + "".join(f"\n\n[作者贡献段 {index}]\n{part}"
                             for index, part in enumerate(passages, 1))


class NoveltyPointExtractorAgent(NoveltyPointExtractor):
    """唯一生产路径为 Harness；输入必须携带持久化 PaperInput 引用。"""

    def __init__(
        self,
        model_client: ModelClient | None = None,
        *,
        prompts: PromptLibrary | None = None,
        models: ModelRegistry | None = None,
        model_alias: str | None = None,
        temperature: float = 0.2,
        model_options: ModelCallOptions | None = None,
        harness_config: ExtractorHarnessConfig | dict | None = None,
    ) -> None:
        self.model_client = model_client
        self._prompts = prompts
        self._models = models
        self._model_alias = model_alias
        self.temperature = temperature
        self.model_options = model_options or ModelCallOptions(temperature=temperature)
        self.harness_config = (harness_config if isinstance(harness_config, ExtractorHarnessConfig)
                               else ExtractorHarnessConfig(**(harness_config or {})))
        self.last_trace: dict[str, Any] = {}

    def extract(
        self,
        digest: PaperDigest,
        *,
        previous_brief: NoveltyBrief | None,
        attempt: int,
    ) -> Sequence[NoveltyPoint]:
        # Resolve the model lazily so input integrity failures are recorded even
        # when no model client is configured. Every request still uses one client.
        harness = ExtractorHarness(self, config=self.harness_config,
            prompts=self._prompts, model_options=self.model_options)
        self.last_trace = harness.trace
        return harness.run(digest)

    def complete(self, messages, *, options=None):
        return self._client().complete(messages, options=options)

    def _client(self) -> ModelClient:
        if self.model_client is not None:
            return self.model_client
        if self._models is not None:
            return self._models.client_for(self._model_alias or "point_extractor")
        raise NotImplementedError("NoveltyPointExtractorAgent 需要注入 ModelClient 或 ModelRegistry")


class DemoPointExtractor(NoveltyPointExtractor):
    """规则版查新点提取：用作者声明贡献或摘要句子生成确定性查新点。"""

    def extract(
        self,
        digest: PaperDigest,
        *,
        previous_brief: NoveltyBrief | None,
        attempt: int,
    ) -> Sequence[NoveltyPoint]:
        claims = (
            list(digest.claimed_contributions)
            or _split_sentences(digest.abstract)
            or ([digest.title] if digest.title else [])
        )
        source = "claimed_contributions" if digest.claimed_contributions else "abstract"
        return [
            NoveltyPoint(
                point_id=f"NP-{index}",
                claim=claim.strip(),
                technical_features=[claim.strip()],
                source_locations=[source],
            )
            for index, claim in enumerate(claims[:DEMO_MAX_POINTS], start=1)
        ]


def _split_sentences(text: str) -> list[str]:
    compact = re.sub(r"\s+", "", text)
    parts = re.split(r"[。；]+", compact)
    return [part for part in parts if len(part) >= 8][:DEMO_MAX_POINTS]
