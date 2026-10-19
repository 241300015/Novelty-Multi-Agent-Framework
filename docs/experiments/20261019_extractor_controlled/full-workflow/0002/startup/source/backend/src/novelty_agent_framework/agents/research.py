"""查新文献调研 Agent：候选文献阅读、比较与证据绑定校验。"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from typing import Any

from pydantic import TypeAdapter, ValidationError

from backend.env import (
    ChatMessage,
    ModelCallOptions,
    ModelClient,
    ModelRegistry,
    PromptLibrary,
)

from ..ports import (
    FullTextTool,
    LiteratureResearchAgent,
    MetadataTool,
    SearchHit,
)
from ..schemas import (
    EvidenceCard,
    EvidenceSource,
    NoveltyPoint,
    ResearchTask,
    ResearcherAction,
)

_ACTION_ADAPTER = TypeAdapter(ResearcherAction)


class NoveltyResearchAgent(LiteratureResearchAgent):
    """逐篇分析上游召回的候选文献并抽取查新点级证据。"""

    def __init__(
        self,
        model_client: ModelClient | None = None,
        *,
        prompts: PromptLibrary | None = None,
        models: ModelRegistry | None = None,
        model_alias: str | None = None,
        temperature: float = 0.2,
        candidate_excerpt_chars: int = 2000,
    ) -> None:
        self.model_client = model_client
        self._prompts = prompts
        self._models = models
        self._model_alias = model_alias
        self.temperature = temperature
        self.candidate_excerpt_chars = candidate_excerpt_chars

    def research(
        self,
        task: ResearchTask,
        point: NoveltyPoint,
        candidates: Sequence[SearchHit],
        *,
        full_text_tool: FullTextTool | None = None,
        metadata_tool: MetadataTool | None = None,
    ) -> Sequence[EvidenceCard]:
        """兼容旧调用方；生产工作流使用 :meth:`decide`。"""
        if task.novelty_point_id != point.point_id:
            raise ValueError(
                "ResearchTask.novelty_point_id 与 NoveltyPoint.point_id 不一致"
            )
        if not candidates:
            return []

        pack = self._build_evidence_pack(candidates, full_text_tool, metadata_tool)
        try:
            data = self._complete_cards(task, point, pack)
        except ValueError:
            # 模型偶发返回非法 JSON：只重试一次输出解析。
            data = self._complete_cards(task, point, pack)
        return self._validate_card_binding(data, task, point, pack)

    async def decide(self, state: dict[str, Any]) -> ResearcherAction:
        """为单个 ResearchTask 产生一个严格 JSON 动作，不执行工具。"""

        request = state["request"]
        payload = {
            "novelty_point": request.novelty_point.model_dump(mode="json"),
            "research_task": request.research_task.model_dump(mode="json"),
            "tools": state.get("tool_descriptions", []),
            "observations": state.get("observations", []),
            "steps_used": state.get("steps_used", 0),
            "remaining_budget": state.get("remaining_budget", {}),
        }
        if self._prompts is not None:
            rendered = self._prompts.render(
                "research/decide",
                state_json=json.dumps(payload, ensure_ascii=False),
                action_schema=json.dumps(
                    _ACTION_ADAPTER.json_schema(), ensure_ascii=False
                ),
            )
            system, user = rendered.system, rendered.user
        else:
            system = (
                "你是单任务 Researcher。每次只输出一个严格 JSON 动作；"
                "可调用已注册工具或提交 finish，不得编造引用。"
            )
            user = json.dumps(payload, ensure_ascii=False)
        messages = [
            ChatMessage(role="system", content=system),
            ChatMessage(role="user", content=user),
        ]
        last_error: Exception | None = None
        for attempt in range(2):
            try:
                client = self._client()
                if hasattr(client, "acomplete"):
                    response = await client.acomplete(
                        messages,
                        options=ModelCallOptions(
                            temperature=self.temperature,
                            response_format={"type": "json_object"},
                        ),
                    )
                else:
                    response = client.complete(
                        messages,
                        options=ModelCallOptions(
                            temperature=self.temperature,
                            response_format={"type": "json_object"},
                        ),
                    )
                return _ACTION_ADAPTER.validate_python(json.loads(response.content))
            except (json.JSONDecodeError, ValidationError, ValueError) as exc:
                last_error = exc
                if attempt == 0:
                    messages.append(
                        ChatMessage(
                            role="user",
                            content="上一响应不符合动作 schema，请只返回一个合法 JSON 动作。",
                        )
                    )
        raise ValueError(f"Researcher 连续返回非法动作：{last_error}")

    def _build_evidence_pack(
        self,
        candidates: Sequence[SearchHit],
        full_text_tool: FullTextTool | None,
        metadata_tool: MetadataTool | None,
    ) -> list[dict[str, Any]]:
        pack: list[dict[str, Any]] = []
        for hit in candidates:
            full_text = self._safe_fetch(full_text_tool, hit.document_id)
            metadata = self._safe_resolve(metadata_tool, hit.document_id)
            source = metadata or EvidenceSource(
                title=hit.title,
                doi=hit.doi,
                url=hit.url,
            )
            uses_full_text = full_text is not None and bool(full_text.text.strip())
            body = full_text.text if uses_full_text else (hit.abstract or "")
            pack.append(
                {
                    "document_id": hit.document_id,
                    "title": source.title or hit.title,
                    "abstract": hit.abstract or "",
                    "excerpt": body[: self.candidate_excerpt_chars],
                    "authors": list(hit.authors),
                    "year": hit.year,
                    "doi": source.doi or hit.doi,
                    "url": source.url or hit.url,
                    "text_source": "full_text" if uses_full_text else "abstract",
                }
            )
        return pack

    def _complete_cards(
        self,
        task: ResearchTask,
        point: NoveltyPoint,
        pack: Sequence[dict[str, Any]],
    ) -> Any:
        payload = {
            "task": task.model_dump(mode="json"),
            "point": point.model_dump(mode="json"),
            "candidates": list(pack),
        }
        return self._complete_json(
            prompt_name="research/literature_review",
            variables={
                "task_json": json.dumps(payload["task"], ensure_ascii=False),
                "point_json": json.dumps(payload["point"], ensure_ascii=False),
                "candidates_json": json.dumps(payload["candidates"], ensure_ascii=False),
                "evidence_schema": json.dumps(
                    EvidenceCard.model_json_schema(), ensure_ascii=False
                ),
            },
            payload=payload,
            fallback_user_prompt=(
                "请逐篇比较候选文献与当前 NoveltyPoint，输出 EvidenceCard 列表；"
                "每张卡只能对应一篇候选文献，quote 必须来自提供的文本。"
            ),
        )

    def _validate_card_binding(
        self,
        data: Any,
        task: ResearchTask,
        point: NoveltyPoint,
        pack: Sequence[dict[str, Any]],
    ) -> list[EvidenceCard]:
        data = _normalize_card_list(data)
        if not isinstance(data, list):
            raise ValueError("NoveltyResearchAgent 返回 JSON 顶层必须是列表")

        seen_card_ids: set[str] = set()
        seen_document_ids: set[str] = set()
        bound: list[EvidenceCard] = []
        for item in data:
            try:
                card = EvidenceCard.model_validate(item)
            except ValidationError:
                continue
            if card.task_id != task.task_id:
                continue
            if card.novelty_point_id != point.point_id:
                continue
            candidate = _match_pack_source(card, pack)
            if candidate is None or not _quotes_are_grounded(card, candidate):
                continue
            document_id = candidate["document_id"]
            if card.card_id in seen_card_ids or document_id in seen_document_ids:
                continue
            seen_card_ids.add(card.card_id)
            seen_document_ids.add(document_id)
            bound.append(card.model_copy(update={"cited_by_paper": None}))
        return bound

    @staticmethod
    def _safe_fetch(tool: FullTextTool | None, document_id: str) -> Any:
        if tool is None:
            return None
        try:
            return tool.fetch(document_id)
        except Exception:
            return None

    @staticmethod
    def _safe_resolve(tool: MetadataTool | None, document_id: str) -> Any:
        if tool is None:
            return None
        try:
            return tool.resolve(document_id)
        except Exception:
            return None

    def _complete_json(
        self,
        *,
        prompt_name: str,
        variables: dict[str, Any],
        payload: dict[str, Any],
        fallback_user_prompt: str,
    ) -> Any:
        client = self._client()
        if self._prompts is not None:
            rendered = self._prompts.render(prompt_name, **variables)
            system, user = rendered.system, rendered.user
        else:
            system = self._system_prompt()
            user = (
                f"{fallback_user_prompt}\n\n输入数据：\n"
                f"{json.dumps(payload, ensure_ascii=False)}"
            )

        response = client.complete(
            [
                ChatMessage(role="system", content=system),
                ChatMessage(role="user", content=user),
            ],
            options=ModelCallOptions(temperature=self.temperature),
        )
        try:
            return json.loads(response.content)
        except json.JSONDecodeError as exc:
            raise ValueError("NoveltyResearchAgent 返回内容不是合法 JSON") from exc

    def _client(self) -> ModelClient:
        if self.model_client is not None:
            return self.model_client
        if self._models is not None:
            return self._models.client_for(self._model_alias or "research")
        raise NotImplementedError(
            "NoveltyResearchAgent 需要注入 ModelClient 或 ModelRegistry"
        )

    @staticmethod
    def _system_prompt() -> str:
        return (
            "你是论文查新系统的文献证据分析 Agent。候选文献已经由上游检索系统"
            "提供。你负责逐篇阅读候选文献，与给定 NoveltyPoint 单独比较，提取技术"
            "重合、技术差异及可追溯原文证据，并输出 EvidenceCard。你不能自行检索、"
            "补充或编造候选文献，也不能给出最终新颖性结论。"
        )


def _normalize_card_list(data: Any) -> Any:
    """兼容单卡对象和常见的列表包装形态。"""

    if isinstance(data, dict):
        for key in ("evidence_cards", "cards"):
            if isinstance(data.get(key), list):
                return data[key]
        return [data]
    return data


def _match_pack_source(
    card: EvidenceCard,
    pack: Sequence[dict[str, Any]],
) -> dict[str, Any] | None:
    """按 DOI、URL、精确规范化标题的优先级绑定候选文献。"""

    for source in card.sources:
        doi = _normalize_doi(source.doi)
        if doi:
            for candidate in pack:
                if doi == _normalize_doi(candidate.get("doi")):
                    return candidate
    for source in card.sources:
        url = _normalize_url(source.url)
        if url:
            for candidate in pack:
                if url == _normalize_url(candidate.get("url")):
                    return candidate
    titles = [_normalize_text(source.title) for source in card.sources]
    titles.append(_normalize_text(card.document_title))
    for title in filter(None, titles):
        for candidate in pack:
            if title == _normalize_text(candidate.get("title", "")):
                return candidate
    return None


def _quotes_are_grounded(card: EvidenceCard, candidate: dict[str, Any]) -> bool:
    candidate_text = _normalize_text(
        f"{candidate.get('abstract', '')} {candidate.get('excerpt', '')}"
    )
    quotes = [source.quote for source in card.sources if source.quote]
    return bool(quotes) and all(
        _normalize_text(quote) in candidate_text for quote in quotes
    )


def _normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip().casefold()


def _normalize_url(value: str | None) -> str:
    return (value or "").strip().casefold().rstrip("/")


def _normalize_doi(value: str | None) -> str:
    normalized = (value or "").strip().casefold()
    for prefix in ("https://doi.org/", "http://doi.org/", "doi:"):
        if normalized.startswith(prefix):
            return normalized[len(prefix) :].strip()
    return normalized
