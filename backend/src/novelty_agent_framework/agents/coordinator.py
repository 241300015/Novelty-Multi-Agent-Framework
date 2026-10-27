"""查新主 Agent。

该文件只描述主 Agent 如何组织论文理解、查新点拆解、补充检索规划和
最终查新结论汇总。真实模型、模型供应商和 API 调用方式由 `backend.env`
统一提供，避免不同开发者在 Agent 内部各写一套模型调用代码。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path

from backend.env import (
    ChatMessage,
    ModelCallOptions,
    ModelClient,
    ModelRegistry,
    PromptLibrary,
)
from ..response_language import with_chinese_explanations
from ..schemas import (
    EvidenceCard,
    InsufficientFinalEvidence,
    NoveltyBrief,
    NoveltyPoint,
    NoveltyPointReview,
    NoveltyReport,
    PaperInput,
    ResearchTask,
)
from ..ports import AsyncNoveltyCoordinator
from ..core.report_synthesis import CoordinatorSynthesisHarness
from ..core.recovery_planning import SUPPLEMENT_ACTIONS
from ..core.coordinator_harness import CoordinatorRecoveryHarness
from ..core.tool_call_harness import ToolCallHarnessConfig
from ..core.supplement_input import build_supplement_input
from ..schemas.supplement_planning import SupplementPlanningInput, SupplementPlanningResult
from ..schemas.failures import RecoveryAction, RecoveryAttempt, RecoveryDecision


class NoveltyCoordinatorAgent(AsyncNoveltyCoordinator):
    """负责查新任务中的全局判断和信息汇总。

    它是查新 Multi-Agent 的主 Agent，但不直接检索文献，也不直接校验证据。
    它的职责是：

    1. 读取论文输入，拆出需要被查证的创新点；
    2. 把创新点转化为可并行执行的文献调研任务；
    3. 根据证据缺口安排补充检索；
    4. 汇总 Research Agent 和 Evidence Validator 的结果，形成报告。
    """

    def __init__(
        self,
        model_client: ModelClient | None = None,
        *,
        prompts: PromptLibrary | None = None,
        models: ModelRegistry | None = None,
        model_alias: str | None = None,
        temperature: float = 0.2,
        model_options: ModelCallOptions | None = None,
        research_languages: Sequence[str] = ("en",),
        supplement_prompt_name: str = "coordinator/supplement",
        synthesis_prompt_name: str = "coordinator/synthesize",
        supplement_harness_config: ToolCallHarnessConfig | None = None,
    ) -> None:
        self.model_client = model_client
        self._prompts = prompts
        self._models = models
        self._model_alias = model_alias
        self.temperature = temperature
        self.model_options = model_options
        self.supplement_prompt_name = supplement_prompt_name
        self.synthesis_prompt_name = synthesis_prompt_name
        self.supplement_harness_config = supplement_harness_config
        languages = tuple(dict.fromkeys(research_languages))
        if not languages or any(item not in {"en", "zh"} for item in languages):
            raise ValueError("research_languages must contain en and/or zh")
        self.research_languages = languages

    def plan(
        self,
        paper: PaperInput,
        *,
        points: Sequence[NoveltyPoint],
        attempt: int,
    ) -> NoveltyBrief:
        """按配置的检索语言分配首轮任务并组装查新规划。

        首轮分工是确定性的，不调用模型，也不生成检索词或 SearchPlan。
        """

        tasks = [
            task
            for point in points
            for task in _initial_tasks_for_point(
                point, attempt=attempt, languages=self.research_languages
            )
        ]
        return NoveltyBrief(
            paper_summary=paper.abstract or paper.title,
            research_problem=paper.title,
            novelty_points=list(points),
            keywords_zh=list(paper.keywords_zh)
            or ([paper.title] if paper.title else []),
            keywords_en=list(paper.keywords_en),
            research_tasks=tasks,
        )

    def plan_supplement(
        self,
        paper: PaperInput,
        *,
        brief: NoveltyBrief,
        existing_evidence: Sequence[EvidenceCard],
        insufficient_final_evidence_points: Sequence[InsufficientFinalEvidence],
        attempt: int,
        recovery_decisions: Sequence[RecoveryDecision] | None = None,
        research_history: Sequence[RecoveryAttempt] = (),
        planning_input: SupplementPlanningInput | None = None,
        material_root: str | Path = "outputs",
    ) -> SupplementPlanningResult:
        """Use the LLM to plan complete tasks for runtime-authorized recovery needs."""
        # Compatibility for direct count-only callers; the workflow always
        # supplies explicit decisions for all three trigger types.
        decisions = list(recovery_decisions) if recovery_decisions is not None else [
            RecoveryDecision(point_id=item.novelty_point_id, action=RecoveryAction.RESEARCH_GAP,
                reason="Final valid evidence count is below the configured threshold.",
                trigger_codes=["evidence.missing"], round_number=max(1, attempt - 1))
            for item in insufficient_final_evidence_points
        ]
        active = [d for d in decisions if d.action in SUPPLEMENT_ACTIONS]
        expected = build_supplement_input(
            paper=paper, brief=brief, decisions=active, evidence=existing_evidence,
            gaps=insufficient_final_evidence_points, languages=self.research_languages,
            attempt=attempt, history=research_history,
        )
        if planning_input is not None:
            planning_input = SupplementPlanningInput.model_validate(planning_input)
            if planning_input.model_dump(exclude={"run_id", "material_index"}) != expected.model_dump(
                exclude={"run_id", "material_index"}
            ):
                raise ValueError("SupplementPlanningInput 与本次运行输入不一致")
        else:
            planning_input = expected
        harness = CoordinatorRecoveryHarness(
            self._client(), prompts=self._prompts or PromptLibrary(Path(__file__).resolve().parents[1] / "prompts"),
            output_root=material_root, config=self.supplement_harness_config,
        )
        return asyncio.run(harness.run(request=planning_input, brief=brief,
            prompt_name=self.supplement_prompt_name,
            options=self.model_options or ModelCallOptions(temperature=self.temperature)))

    def synthesize(
        self, paper: PaperInput, *, brief: NoveltyBrief,
        evidence: Sequence[EvidenceCard], novelty_reviews: Sequence[NoveltyPointReview],
        rejected_evidence: Sequence[str],
        insufficient_final_evidence_points: Sequence[InsufficientFinalEvidence],
    ) -> NoveltyReport:
        """Synchronous compatibility entry; the workflow uses asynthesize."""
        return asyncio.run(self.asynthesize(paper, brief=brief, evidence=evidence,
            novelty_reviews=novelty_reviews, rejected_evidence=rejected_evidence,
            insufficient_final_evidence_points=insufficient_final_evidence_points))

    async def asynthesize(
        self, paper: PaperInput, *, brief: NoveltyBrief,
        evidence: Sequence[EvidenceCard], novelty_reviews: Sequence[NoveltyPointReview],
        rejected_evidence: Sequence[str],
        insufficient_final_evidence_points: Sequence[InsufficientFinalEvidence],
        raw_evidence=(), max_concurrency: int = 4,
        output_root: str | Path | None = None, run_id: str | None = None,
    ) -> NoveltyReport:
        """Harness partitions upstream materials, joins all points, then persists."""
        harness = CoordinatorSynthesisHarness(self.synthesize_point,
            max_concurrency=max_concurrency,
            max_tokens=(self.model_options or ModelCallOptions()).max_tokens)
        return await harness.run(paper, brief=brief, evidence=evidence,
            novelty_reviews=novelty_reviews, rejected_evidence=rejected_evidence,
            insufficient_final_evidence_points=insufficient_final_evidence_points,
            raw_evidence=raw_evidence, output_root=output_root, run_id=run_id)

    async def synthesize_point(self, materials, *, schema, repair_feedback=None):
        """Write prose for exactly one point; all identifiers remain Harness-owned."""
        prompts = self._prompts or PromptLibrary(Path(__file__).resolve().parents[1] / "prompts")
        rendered = prompts.render(self.synthesis_prompt_name,
            materials_json=json.dumps(materials, ensure_ascii=False),
            draft_schema=json.dumps(schema, ensure_ascii=False))
        system = with_chinese_explanations(rendered.system)
        if repair_feedback is not None:
            system += ("\n当前为 Harness 字段级纠错阶段。仅返回当前 option_id 和 updates；"
                       "按 fields 中的编号修改，程序保留其他字段并组装。只能使用当前 schema，"
                       "不能返回完整报告、点标识或卡片分组。")
        messages = [ChatMessage(role="system", content=system),
                    ChatMessage(role="user", content=rendered.user)]
        if repair_feedback is not None:
            messages.append(ChatMessage(role="user", content=json.dumps(repair_feedback, ensure_ascii=False)))
        options = replace(self.model_options or ModelCallOptions(temperature=self.temperature),
            tools=(), tool_choice="none", response_format={"type": "json_schema", "json_schema": {
                "name": "point_narrative_repair" if repair_feedback is not None else "point_narrative",
                "strict": True, "schema": schema}})
        client = self._client()
        if callable(getattr(client, "acomplete", None)):
            return await client.acomplete(messages, options=options)
        return await asyncio.to_thread(client.complete, messages, options=options)

    def _client(self) -> ModelClient:
        if self.model_client is not None:
            return self.model_client
        if self._models is not None:
            return self._models.client_for(self._model_alias or "coordinator")
        raise NotImplementedError(
            "NoveltyCoordinatorAgent 需要注入 ModelClient 或 ModelRegistry"
        )


def _initial_tasks_for_point(
    point: NoveltyPoint,
    *,
    attempt: int,
    languages: Sequence[str],
) -> tuple[ResearchTask, ...]:
    """为单个查新点按启用语言创建首轮任务。"""

    descriptions = {
        "en": "针对该查新点执行英文文献检索。",
        "zh": "针对该查新点执行中文文献检索。",
    }
    return tuple(
        ResearchTask(
            task_id=f"T-{index}",
            novelty_point_id=point.point_id,
            task_type="literature_search",
            language=language,
            description=descriptions[language],
            attempt=attempt,
        )
        for index, language in enumerate(languages, 1)
    )
