"""SearchPlanner Agent：把查新点和调研任务转换为数据库无关的检索计划。

模型只输出最小契约 SearchPlanDraft v2（concepts.role/terms/alias/exclude/importance
 + strategies.level/focus_concepts）；布尔表达式由编译器模板生成；
机械字段（concept_id/strategy_id/task_id/novelty_point_id/level/name/description）
由 search_plan_compiler.build_runtime_plan 确定性补全，模型不参与生成。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from pydantic import ValidationError

from backend.env import (
    ChatMessage,
    ModelCallOptions,
    ModelClient,
    ModelClientError,
    ModelRegistry,
    PromptLibrary,
)

from ..ports import SearchPlanner
from ..schemas import NoveltyPoint, ResearchTask, SearchPlan, SearchPlanDraft
from .search_plan_compiler import (
    SearchPlanCompilationError,
    SemanticLimits,
    build_runtime_plan,
)


class SearchPlannerExhaustedError(ValueError):
    """SearchPlanner exhausted its bounded retries with audit-ready context."""

    def __init__(
        self,
        *,
        novelty_point_id: str,
        task_id: str,
        attempts: int,
        last_error: str,
        failure_category: str,
    ) -> None:
        self.novelty_point_id = novelty_point_id
        self.task_id = task_id
        self.attempts = attempts
        self.last_error = last_error
        self.failure_category = failure_category
        self.audit = {
            "novelty_point_id": novelty_point_id,
            "task_id": task_id,
            "attempts": attempts,
            "last_error": last_error,
            "failure_category": failure_category,
        }
        super().__init__(
            "SearchPlanner retries exhausted: "
            + json.dumps(self.audit, ensure_ascii=False, sort_keys=True)
        )


class SearchPlannerAgent(SearchPlanner):
    """用 LLM 生成最小契约 SearchPlanDraft，并补全为运行时 SearchPlan。"""

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

        last_error: Exception | None = None
        failure_category = "unknown"
        retry_reason = ""
        from ..core.runtime_artifacts import current_runtime_artifacts
        runtime = current_runtime_artifacts()

        def record(attempt: int, step: str, payload: Any) -> None:
            if runtime is not None:
                runtime.record_planner_event({
                    "point_id": point.point_id, "task_id": task.task_id,
                    "attempt": attempt, "step": step, "payload": payload,
                })

        for attempt_index in range(self.max_attempts):
            attempt = attempt_index + 1
            try:
                if use_async_client:
                    data = await self._acomplete_json(
                        point=point, task=task, retry_reason=retry_reason,
                    )
                else:
                    data = self._complete_json(
                        point=point, task=task, retry_reason=retry_reason,
                    )
                record(attempt, "parsed_model_json", data)
                draft = _validate_draft_data(data)
                record(attempt, "validated_draft", draft)
                plan = build_runtime_plan(
                    draft, task=task, limits=self.semantic_limits
                )
                record(attempt, "compiled_plan", plan)
                return plan
            except SearchPlanCompilationError as exc:
                record(attempt, "compilation_failed", {
                    "error": str(exc), "issues": exc.issues,
                })
                last_error = exc
                failure_category = "plan_compilation_error"
                retry_reason = _format_compilation_feedback(exc)
            except ModelClientError as exc:
                record(attempt, "model_call_failed", {"error": str(exc)})
                last_error = exc
                failure_category = "model_client_error"
                retry_reason = f"模型网络调用失败：{exc}（将重试）"
            except ValueError as exc:
                record(attempt, "draft_validation_failed", {"error": str(exc)})
                last_error = exc
                failure_category = "invalid_model_output"
                retry_reason = f"格式校验失败：{exc}"
        raise SearchPlannerExhaustedError(
            novelty_point_id=point.point_id,
            task_id=task.task_id,
            attempts=self.max_attempts,
            last_error=str(last_error or "未知错误"),
            failure_category=failure_category,
        ) from last_error

    async def _acomplete_json(
        self, *, point: NoveltyPoint, task: ResearchTask, retry_reason: str,
    ) -> Any:
        messages = self._build_messages(point=point, task=task, retry_reason=retry_reason)
        response = await self._client().acomplete(messages, options=self.model_options)
        return self._parse_response(response.content)

    def _complete_json(
        self, *, point: NoveltyPoint, task: ResearchTask, retry_reason: str,
    ) -> Any:
        messages = self._build_messages(point=point, task=task, retry_reason=retry_reason)
        response = self._client().complete(messages, options=self.model_options)
        return self._parse_response(response.content)

    def _build_messages(
        self,
        *,
        point: NoveltyPoint,
        task: ResearchTask,
        retry_reason: str,
    ) -> list[ChatMessage]:
        payload = {
            "point": point.model_dump(mode="json"),
            "task": task.model_dump(mode="json"),
            "retry_reason": retry_reason,
        }
        variables = {
            "point_json": json.dumps(payload["point"], ensure_ascii=False),
            "task_json": json.dumps(payload["task"], ensure_ascii=False),
            "draft_schema": json.dumps(
                SearchPlanDraft.model_json_schema(), ensure_ascii=False
            ),
            "retry_reason": retry_reason or "无（首次生成）",
        }
        if self._prompts is not None:
            rendered = self._prompts.render(self.prompt_name, **variables)
            system, user = rendered.system, rendered.user
        else:
            system = self._system_prompt()
            user = (
                "请把输入转换为数据库无关的 SearchPlanDraft（v2）JSON。"
                "不得输出数据库字段语法、布尔表达式，也不得输出 concept_id/"
                "strategy_id/expression/description/task_id/novelty_point_id 等系统分配字段。\n\n输入：\n"
                f"{json.dumps(payload, ensure_ascii=False)}\n\n"
                f"输出 schema：\n{variables['draft_schema']}"
            )

        return [
            ChatMessage(role="system", content=system),
            ChatMessage(role="user", content=user),
        ]

    @staticmethod
    def _parse_response(content: str | None) -> Any:
        try:
            return json.loads(content)
        except (json.JSONDecodeError, TypeError) as exc:
            raise ValueError("SearchPlanner 返回内容不是合法 JSON") from exc

    def _client(self) -> ModelClient:
        if self.model_client is not None:
            return self.model_client
        if self._models is not None:
            return self._models.client_for(self._model_alias or "search_planner")
        raise NotImplementedError(
            "SearchPlannerAgent 需要注入 ModelClient 或 ModelRegistry"
        )

    @staticmethod
    def _system_prompt() -> str:
        return (
            "你是科技查新系统中的 SearchPlanner。你只把 NoveltyPoint 和 "
            "ResearchTask 转换为最小检索草稿 SearchPlanDraft（v2）：concepts 的"
            "角色/词项/别名/排除词/重要性，与 strategies 的 level（及可选 "
            "focus_concepts）。不能检索文献、编造来源、输出数据库专用语法、布尔"
            "表达式或任何系统分配字段（concept_id/strategy_id/expression/"
            "description/task_id/novelty_point_id）。"
        )


def _format_compilation_feedback(exc: SearchPlanCompilationError) -> str:
    """把编译器的结构化错误列表转为重试 prompt 的 JSON 反馈。"""

    if exc.issues:
        return json.dumps(
            [
                {"code": issue.code, "detail": issue.detail, "fix": issue.fix}
                for issue in exc.issues
            ],
            ensure_ascii=False,
        )
    return str(exc)


def _validate_draft_data(data: Any) -> SearchPlanDraft:
    if isinstance(data, dict) and isinstance(data.get("search_plan"), dict):
        data = data["search_plan"]
    if not isinstance(data, dict):
        raise ValueError("SearchPlanner 输出顶层必须是 SearchPlanDraft 对象")
    try:
        return SearchPlanDraft.model_validate(data)
    except ValidationError as exc:
        raise ValueError(
            f"SearchPlanner 输出不符合 SearchPlanDraft schema：{exc}"
        ) from exc
