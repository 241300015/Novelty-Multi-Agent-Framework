"""Planner model adapter. Harness accepts concepts before requesting strategies.

The legacy SearchPlanDraft remains the internal compiler/archival format; live
model responses use separate concept and strategy schemas.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import asdict, replace
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any
from pathlib import Path
from ..schemas.search_plan_draft import SearchConceptsDraft

from pydantic import ValidationError

from backend.env import (
    ChatMessage,
    ModelCallOptions,
    ModelClient,
    ModelRegistry,
    PromptLibrary,
)

from ..response_language import with_chinese_explanations
from ..core.planner_repair import PlannerRepairHarness
from ..ports import SearchPlanner
from ..schemas import NoveltyPoint, ResearchTask, SearchPlan, SearchPlanDraft
from ..schemas.failures import CodedFailure, FailureCode as C
from .search_plan_compiler import SemanticLimits


class SearchPlannerExhaustedError(CodedFailure):
    """Terminal planning failure (historical name retained for callers)."""

    def __init__(
        self,
        *,
        novelty_point_id: str,
        task_id: str,
        attempts: int,
        last_error: str,
        failure_category: str,
        code: C = C.MODEL_OUTPUT_SCHEMA,
        terminal_reason: str = "attempt_limit",
        history: list[dict[str, Any]] | None = None,
        planning_stage: str | None = None, catalog_sha256: str | None = None,
    ) -> None:
        self.novelty_point_id = novelty_point_id
        self.task_id = task_id
        self.attempts = attempts
        self.last_error = last_error
        self.failure_category = failure_category
        self.terminal_reason = terminal_reason
        self.history = history or []
        self.planning_stage = planning_stage
        self.audit = {
            "planning_stage": planning_stage, "catalog_sha256": catalog_sha256,
            "novelty_point_id": novelty_point_id,
            "task_id": task_id,
            "attempts": attempts,
            "last_error": last_error,
            "failure_category": failure_category,
            "code": code.value,
            "terminal_reason": terminal_reason,
            "history": self.history,
        }
        super().__init__(
            code, "SearchPlanner failed: "
            + json.dumps(self.audit, ensure_ascii=False, sort_keys=True)
        )


class _DraftOutputError(CodedFailure):
    """Only errors raised at the model draft boundary are repairable outputs."""

    def __init__(self, code: C, message: str, *, output: Any, issues=None):
        super().__init__(code, message)
        self.output = output
        self.issues = issues or [{"code": code.value, "detail": message}]


_TRANSIENT_MODEL_CODES = frozenset({
    C.MODEL_TIMEOUT, C.MODEL_TRANSPORT, C.MODEL_HTTP_RATE, C.MODEL_HTTP_SERVER,
})


class SearchPlannerAgent(SearchPlanner):
    """提供两阶段模型调用；阶段推进、修复和编译由 SearchPlannerHarness 控制。"""

    def __init__(
        self,
        model_client: ModelClient | None = None,
        *,
        prompts: PromptLibrary | None = None,
        models: ModelRegistry | None = None,
        model_alias: str | None = None,
        temperature: float = 0.2,
        model_options: ModelCallOptions | None = None,
        max_attempts: int = 3,
        semantic_limits: SemanticLimits | None = None,
        prompt_name: str = "search_planner/plan",
        concept_prompt: str = "search_planner/concepts",
    ) -> None:
        self.model_client = model_client
        self._prompts = prompts
        self._models = models
        self._model_alias = model_alias
        self.temperature = temperature
        self.model_options = model_options or ModelCallOptions(
            temperature=temperature,
            response_format={"type": "json_object"},
        )
        if max_attempts < 1:
            raise ValueError("max_attempts must be positive")
        self.max_attempts = max_attempts
        self.semantic_limits = semantic_limits
        self.prompt_name = prompt_name
        self.concept_prompt = concept_prompt

    def plan(self, point: NoveltyPoint, task: ResearchTask) -> SearchPlan:
        """将 NoveltyPoint + ResearchTask 转换为经过补全的运行时 SearchPlan。"""

        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(self._plan(point, task, use_async_client=False))
        raise RuntimeError("事件循环中请使用 await planner.aplan(point, task)")

    async def aplan(self, point: NoveltyPoint, task: ResearchTask) -> SearchPlan:
        """异步生成、校验和编译方案；等待模型期间不阻塞其他任务。"""
        return await self._plan(point, task, use_async_client=True)

    async def _plan(
        self, point: NoveltyPoint, task: ResearchTask, *, use_async_client: bool,
    ) -> SearchPlan:
        if task.novelty_point_id != point.point_id:
            raise ValueError(
                "ResearchTask.novelty_point_id 与 NoveltyPoint.point_id 不一致"
            )

        from ..core.planner_harness import SearchPlannerHarness
        return await SearchPlannerHarness(self).run(point, task, use_async_client=use_async_client)

    async def _acomplete_json(
        self, *, point: NoveltyPoint, task: ResearchTask, retry_reason: str,
        stage="concepts", schema=None, catalog=None,
    ) -> Any:
        messages = self._build_messages(point=point, task=task, retry_reason=retry_reason,
                                        stage=stage, schema=schema, catalog=catalog)
        response = await self._client().acomplete(messages, options=self._call_options(retry_reason, schema=schema, stage=stage))
        return self._parse_model_response(response)

    def _complete_json(
        self, *, point: NoveltyPoint, task: ResearchTask, retry_reason: str,
        stage="concepts", schema=None, catalog=None,
    ) -> Any:
        messages = self._build_messages(point=point, task=task, retry_reason=retry_reason,
                                        stage=stage, schema=schema, catalog=catalog)
        response = self._client().complete(messages, options=self._call_options(retry_reason, schema=schema, stage=stage))
        return self._parse_model_response(response)

    def _call_options(self, retry_reason: str, *, schema=None, stage="concepts") -> ModelCallOptions:
        response_format = {"type": "json_schema", "json_schema": {
            "name": "planner_" + stage, "schema": schema or SearchConceptsDraft.model_json_schema()}}
        if retry_reason:
            feedback = json.loads(retry_reason)
            response_format = PlannerRepairHarness.response_format(feedback["options"], feedback.get("fields"), draft_schema=schema)
        return replace(self.model_options, response_format=response_format, tools=(), tool_choice="none")

    def _build_messages(
        self,
        *,
        point: NoveltyPoint,
        task: ResearchTask,
        retry_reason: str,
        stage="concepts", schema=None, catalog=None,
    ) -> list[ChatMessage]:
        payload = {
            "point": point.model_dump(mode="json"),
            "task": task.model_dump(mode="json"),
        }
        variables = {
            "point_json": json.dumps(payload["point"], ensure_ascii=False),
            "task_json": json.dumps(payload["task"], ensure_ascii=False),
            "draft_schema": json.dumps(
                schema or SearchConceptsDraft.model_json_schema(), ensure_ascii=False
            ),
            "retry_reason": retry_reason or "无（首次生成）",
            "concept_catalog_json": json.dumps(catalog.view() if catalog else [], ensure_ascii=False),
        }
        library = self._prompts or PromptLibrary(Path(__file__).resolve().parents[1] / "prompts")
        rendered = library.render(self.concept_prompt if stage == "concepts" else self.prompt_name, **variables)
        system, user = rendered.system, rendered.user
        user += "\nHarness 当前阶段：" + stage
        if catalog is not None:
            user += "\nHarness 固定概念目录（只读）：" + variables["concept_catalog_json"]
        # Also enforce the stage boundary with custom prompt templates.
        system += ("\nHarness 控制阶段。当前只能输出 concepts，完成分类和词项提取；不生成策略。"
                   if stage == "concepts" else
                   "\nHarness 已固定概念目录。当前只能输出 strategies；仅引用提供的概念编号，不修改或重建概念。")
        if variables["draft_schema"] not in system and variables["draft_schema"] not in user:
            system += "\n当前阶段 schema：" + variables["draft_schema"]

        # Effective configuration is authoritative even with a custom prompt.
        user += "\n\n本次实际校验上限（优先于示例中的数量建议）：\n" + json.dumps(
            asdict(self.semantic_limits or SemanticLimits()), ensure_ascii=False)
        if retry_reason:
            if json.loads(retry_reason).get("fields") is not None:
                system += ("\n当前为 Harness 字段级纠错阶段，本段覆盖此前完整草稿输出要求："
                           '只返回 {"option_id":"当前菜单 ID","updates":{"field_0":新值}}，'
                           "updates 只能包含当前 fields 中的编号；程序保留其他字段并组装草稿。"
                           "禁止返回完整 draft、自定义路径或操作。停止时 updates=null。")
            else:
                system += ("\n当前为 Harness 定向修复阶段，输出协议覆盖首次草稿要求："
                           '只返回 {"option_id":"当前菜单 ID","draft":完整草稿或null}。'
                           "必须遵守 allowed_paths，不能自行扩大修改范围。")
        return [
            ChatMessage(role="system", content=with_chinese_explanations(system)),
            ChatMessage(role="user", content=user),
        ]

    @staticmethod
    def _parse_response(content: str | None) -> Any:
        try:
            return json.loads(content)
        except (json.JSONDecodeError, TypeError) as exc:
            detail = "SearchPlanner 返回内容不是合法 JSON"
            if isinstance(exc, json.JSONDecodeError):
                detail += f"：{exc.msg}（line={exc.lineno}, column={exc.colno}）"
            raise _DraftOutputError(C.MODEL_JSON_INVALID, detail, output=content) from exc

    @classmethod
    def _parse_model_response(cls, response):
        choices = response.raw.get("choices", []) if isinstance(response.raw, dict) else []
        if choices and isinstance(choices[0], dict) and choices[0].get("finish_reason") == "length":
            raise _DraftOutputError(C.MODEL_OUTPUT_TRUNCATED,
                "SearchPlanner 输出因长度限制被截断；停止同条件重试", output=response.content)
        if response.tool_calls:
            raise _DraftOutputError(C.PLANNER_TOOL_CALL_FORBIDDEN,
                "SearchPlanner 只允许返回草稿 JSON，不允许工具调用", output={
                    "content": response.content,
                    "tool_calls": [{"name": call.name, "arguments": call.arguments}
                                   for call in response.tool_calls],
                })
        return cls._parse_response(response.content)

    def _client(self) -> ModelClient:
        if self.model_client is not None:
            return self.model_client
        if self._models is not None:
            return self._models.client_for(self._model_alias or "search_planner")
        raise NotImplementedError(
            "SearchPlannerAgent 需要注入 ModelClient 或 ModelRegistry"
        )


def _transport_retry_delay(exc: Exception) -> float:
    """Honor a server Retry-After; a wait over 30 s is handled as terminal here."""
    headers = getattr(exc.__cause__, "headers", None)
    value = headers.get("Retry-After") if headers else None
    if value:
        try:
            return max(0.5, float(value))
        except ValueError:
            try:
                when = parsedate_to_datetime(value)
                return max(0.5, (when - datetime.now(timezone.utc)).total_seconds())
            except (ValueError, TypeError, OverflowError):
                pass
    return 0.5


def _validate_draft_data(data: Any, *, model=SearchPlanDraft, allow_wrapper=True):
    if allow_wrapper and isinstance(data, dict) and isinstance(data.get("search_plan"), dict):
        data = data["search_plan"]
    if not isinstance(data, dict):
        raise _DraftOutputError(C.MODEL_SCHEMA_INVALID,
            f"SearchPlanner 输出顶层必须是 {model.__name__} 对象", output=data)
    try:
        return model.model_validate(data)
    except ValidationError as exc:
        issues = [{"code": C.MODEL_SCHEMA_INVALID.value, "path": list(e["loc"]),
                   "validation_type": e["type"], "detail": e["msg"]}
                  for e in exc.errors(include_url=False, include_context=False, include_input=False)]
        raise _DraftOutputError(C.MODEL_SCHEMA_INVALID,
            f"SearchPlanner 输出不符合 {model.__name__} schema", output=data, issues=issues) from exc
