"""配置驱动的组合根。

读取 ``models`` / ``agents`` 配置，构建 ModelRegistry、PromptLibrary 和各角色
Agent，最后装配成 NoveltyWorkflow。Demo 装配仍由 ``NoveltyWorkflow.default``
保留，供测试和无模型环境使用。
"""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path
from typing import Any, Mapping

from backend.env import ModelCallOptions, ModelProfile, ModelRegistry, PromptLibrary
from backend.env.context_admission import ContextAdmissionConfig

from ..agents import (
    EvidenceReviewerConfig,
    NoveltyCoordinatorAgent,
    NoveltyEvidenceReviewer,
    NoveltyPointExtractorAgent,
    NoveltyResearchAgent,
    SearchPlannerAgent,
)
from ..agents.search_plan_compiler import SemanticLimits
from ..core.runtime_artifacts import RuntimeDebugConfig
from ..tools import (
    BaiduSearchBackend,
    BrowserTool,
    EvidenceCardBuilder,
    PlaywrightBrowserBackend,
    ReferenceArtifactReaderTool,
    ReferenceSearchTool,
    ReaderTool,
    ReviewerReaderTool,
    ResearcherToolRegistry,
    WebSearchTool,
)
from ..tools.database_search import (
    RetrievalSource,
    RetrievalSourceRegistry,
    StructuredSourceRetrievalTool,
)
from ..tools.database_search.factory import (
    build_database_search_tool,
    build_retrieval_source as build_database_retrieval_source,
    build_source_registry as build_database_source_registry,
    build_structured_source_retrieval_tool as build_database_structured_tool,
)
from ..persistence import ReferenceStore, SubjectReferenceStore
from ..workflows import (
    NoveltyWorkflow,
    NoveltyWorkflowConfig,
    NoveltyWorkflowServices,
    TaskResearcherConfig,
    TaskResearcherWorkflow,
)
from .loader import legacy_shape, load_application_config, read_json
from .schemas import ApplicationConfig, ModelInvocationConfig

CONFIG_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG_PATH = CONFIG_DIR / "settings.example.json"
PROMPTS_ROOT = Path(__file__).resolve().parents[1] / "prompts"


def load_config(path: str | Path | None = None) -> dict[str, Any]:
    """Compatibility shape; new code should call load_application_config()."""

    if path is not None:
        return read_json(path)
    return legacy_shape(load_application_config())


def build_model_registry(
    config: Mapping[str, Any] | ApplicationConfig,
) -> ModelRegistry:
    profiles: dict[str, ModelProfile] = {}
    model_items = (
        config.models.items()
        if isinstance(config, ApplicationConfig)
        else config.get("models", {}).items()
    )
    for alias, value in model_items:
        raw = value.model_dump(mode="python") if hasattr(value, "model_dump") else value
        timeout = raw.get("timeout_seconds")
        if timeout is None and not isinstance(config, ApplicationConfig):
            timeout = os.getenv("NOVELTY_TIMEOUT_SECONDS")
        if timeout is None:
            timeout = 60.0
        profiles[alias] = ModelProfile(
            alias=alias,
            provider=raw.get("provider", "openai_compatible"),
            model=raw["model"],
            base_url=raw.get("base_url", "https://api.openai.com/v1"),
            api_key=_resolve_api_key(raw),
            context_window=int(raw.get("context_window", 128_000)),
            context_admission=ContextAdmissionConfig(**raw.get("context_admission", {})),
            supported_params=frozenset(raw.get("supported_params", [])),
            defaults={"timeout_seconds": float(timeout) if timeout else 60.0},
        )
    return ModelRegistry(profiles)


def _resolve_api_key(raw: Mapping[str, Any]) -> str | None:
    env_name = raw.get("api_key_env")
    if env_name:
        value = os.getenv(env_name)
        if value:
            return value
    return os.getenv("NOVELTY_API_KEY") or os.getenv("LLM_API_KEY")


def build_prompt_library(root: str | Path | None = None) -> PromptLibrary:
    return PromptLibrary(root or PROMPTS_ROOT)


def build_agents(
    config: Mapping[str, Any],
    registry: ModelRegistry,
    prompts: PromptLibrary,
    retrieval_cfg: Mapping[str, Any] | None = None,
) -> tuple[
    NoveltyCoordinatorAgent,
    NoveltyResearchAgent,
    NoveltyPointExtractorAgent,
    SearchPlannerAgent,
]:
    agents_cfg = config.get("agents", {})
    coordinator_cfg = agents_cfg.get("coordinator", {})
    research_cfg = agents_cfg.get("research", {})
    point_extractor_cfg = agents_cfg.get("point_extractor", {})
    search_planner_cfg = agents_cfg.get("search_planner", {})
    retrieval_cfg = dict(retrieval_cfg or {})

    coordinator = NoveltyCoordinatorAgent(
        prompts=prompts,
        models=registry,
        model_alias=coordinator_cfg.get("model", "coordinator"),
        supplement_prompt_name=coordinator_cfg.get("prompt_names", {}).get("supplement", "coordinator/supplement"),
        synthesis_prompt_name=coordinator_cfg.get("prompt_names", {}).get("synthesize", "coordinator/synthesize"),
        temperature=float(coordinator_cfg.get("temperature", 0.2)),
        model_options=(_model_options(coordinator_cfg["model_options"], response_format={"type": "json_object"})
                       if coordinator_cfg.get("model_options") else None),
        research_languages=config.get("workflow", {}).get(
            "research_languages", ("en",)
        ),
    )
    research = NoveltyResearchAgent(
        prompts=prompts,
        models=registry,
        model_alias=research_cfg.get("model", "research"),
        temperature=float(research_cfg.get("temperature", 0.2)),
        candidate_excerpt_chars=int(retrieval_cfg.get("candidate_excerpt_chars", 2000)),
    )
    point_extractor = NoveltyPointExtractorAgent(
        prompts=prompts,
        models=registry,
        model_alias=point_extractor_cfg.get("model", "point_extractor"),
        conservative_dedup=bool(point_extractor_cfg.get("conservative_dedup", True)),
        generation_prompt_name=point_extractor_cfg.get("prompt_names", {}).get("generate", "extractor/extract_points"),
        review_prompt_name=point_extractor_cfg.get("prompt_names", {}).get("deduplicate", "reviewer/review_points"),
        coverage_prompt_name=point_extractor_cfg.get("prompt_names", {}).get("coverage", "extractor/extract_points"),
        temperature=float(point_extractor_cfg.get("temperature", 0.2)),
        model_options=(_model_options(point_extractor_cfg["model_options"], response_format={"type": "json_object"})
                       if point_extractor_cfg.get("model_options") else None),
    )
    search_planner = build_search_planner(
        config, registry, prompts, retrieval_cfg=retrieval_cfg
    )
    return coordinator, research, point_extractor, search_planner


def build_search_planner(
    config: Mapping[str, Any],
    registry: ModelRegistry,
    prompts: PromptLibrary,
    *,
    retrieval_cfg: Mapping[str, Any] | None = None,
) -> SearchPlannerAgent:
    """单独构建 SearchPlanner，供检索 Tool 组合根复用。"""

    agents_cfg = config.get("agents", {})
    research_cfg = agents_cfg.get("research", {})
    search_planner_cfg = agents_cfg.get("search_planner", {})
    runtime = config.get("search_planner_runtime", {})
    invocation = runtime.get("model", {})
    return SearchPlannerAgent(
        prompts=prompts,
        models=registry,
        model_alias=search_planner_cfg.get(
            "model", research_cfg.get("model", "search_planner")
        ),
        temperature=float(search_planner_cfg.get("temperature", 0.2)),
        model_options=(
            _model_options(invocation, response_format={"type": "json_object"})
            if invocation else None
        ),
        max_attempts=int(runtime.get("max_attempts", 3)),
        semantic_limits=SemanticLimits(**(runtime.get("limits") or {})),
        prompt_name=str(runtime.get("prompt", "search_planner/plan")),
    )


def build_source_registry() -> RetrievalSourceRegistry:
    """兼容入口；数据库组合职责已收束到 database_search。"""

    return build_database_source_registry()


def build_retrieval_source(
    config: Mapping[str, Any],
    *,
    source_registry: RetrievalSourceRegistry | None = None,
) -> RetrievalSource:
    return build_database_retrieval_source(
        _normalized_retrieval_config(config), source_registry=source_registry
    )


def build_tools(config: Mapping[str, Any]):
    """兼容旧调用者，返回活动来源的三项检索工具。"""

    source = build_retrieval_source(config)
    return source.search_tool, source.full_text_tool, source.metadata_tool


def build_structured_source_retrieval_tool(
    config: Mapping[str, Any],
    *,
    source_registry: RetrievalSourceRegistry | None = None,
) -> StructuredSourceRetrievalTool:
    """构建独立检索 Tool；不接入或修改现有 NoveltyWorkflow。"""

    raw = copy.deepcopy(dict(config))
    _apply_env_overrides(raw)
    models = build_model_registry(raw)
    prompts = build_prompt_library()
    retrieval_cfg = _normalized_retrieval_config(raw)
    search_planner = build_search_planner(
        raw, models, prompts, retrieval_cfg=retrieval_cfg
    )
    workflow_cfg = raw.get("workflow", {})
    return build_database_structured_tool(
        retrieval_cfg,
        search_planner=search_planner,
        source_registry=source_registry,
        max_concurrency=int(workflow_cfg.get("max_concurrency", 4)),
    )


def build_workflow(
    config: Mapping[str, Any] | ApplicationConfig | None = None,
    *,
    config_path: str | Path | None = None,
    source_registry: RetrievalSourceRegistry | None = None,
    output_root: str | Path | None = None,
) -> NoveltyWorkflow:
    """从配置构建完整工作流；``config`` 优先于 ``config_path``。"""

    if isinstance(config, ApplicationConfig):
        return _build_workflow_from_application_config(
            config, source_registry=source_registry, output_root=output_root
        )
    if config is None and config_path is None:
        return _build_workflow_from_application_config(
            load_application_config(),
            source_registry=source_registry,
            output_root=output_root,
        )

    raw = (
        load_config(config_path) if config is None else copy.deepcopy(dict(config))
    )
    _apply_env_overrides(raw)
    timeout_origins = {}
    for alias, model in raw.get("models", {}).items():
        if model.get("timeout_seconds") is None:
            model["timeout_seconds"] = float(os.getenv("NOVELTY_TIMEOUT_SECONDS") or 60.0)
            timeout_origins[f"models.{alias}.timeout_seconds"] = "environment" if os.getenv("NOVELTY_TIMEOUT_SECONDS") else "schema_default"
        else:
            timeout_origins[f"models.{alias}.timeout_seconds"] = "legacy_config"
    origins = raw.setdefault("configuration_resolution", {}).setdefault("field_sources", {})
    for field, source in timeout_origins.items():
        origins.setdefault(field, source)

    registry = build_model_registry(raw)
    prompts = build_prompt_library()
    retrieval_cfg = _normalized_retrieval_config(raw)
    if "retrieval" not in raw:
        retrieval_cfg.setdefault("sources", {}).setdefault("arxiv", {})[
            "adapter_only"
        ] = False
    agents_cfg = raw.get("agents", {})
    coordinator_cfg = agents_cfg.get("coordinator", {})
    point_extractor_cfg = agents_cfg.get("point_extractor", {})
    research_cfg = agents_cfg.get("research", {})
    coordinator = NoveltyCoordinatorAgent(
        prompts=prompts,
        models=registry,
        model_alias=coordinator_cfg.get("model", "coordinator"),
        supplement_prompt_name=coordinator_cfg.get("prompt_names", {}).get("supplement", "coordinator/supplement"),
        synthesis_prompt_name=coordinator_cfg.get("prompt_names", {}).get("synthesize", "coordinator/synthesize"),
        temperature=float(coordinator_cfg.get("temperature", 0.2)),
        model_options=(_model_options(coordinator_cfg["model_options"], response_format={"type": "json_object"})
                       if coordinator_cfg.get("model_options") else None),
        research_languages=raw.get("workflow", {}).get(
            "research_languages", ("en",)
        ),
    )
    point_extractor = NoveltyPointExtractorAgent(
        prompts=prompts,
        models=registry,
        model_alias=point_extractor_cfg.get("model", "point_extractor"),
        conservative_dedup=bool(point_extractor_cfg.get("conservative_dedup", True)),
        generation_prompt_name=point_extractor_cfg.get("prompt_names", {}).get("generate", "extractor/extract_points"),
        review_prompt_name=point_extractor_cfg.get("prompt_names", {}).get("deduplicate", "reviewer/review_points"),
        coverage_prompt_name=point_extractor_cfg.get("prompt_names", {}).get("coverage", "extractor/extract_points"),
        temperature=float(point_extractor_cfg.get("temperature", 0.2)),
        model_options=(_model_options(point_extractor_cfg["model_options"], response_format={"type": "json_object"})
                       if point_extractor_cfg.get("model_options") else None),
    )
    research_model = registry.client_for(research_cfg.get("model", "research"))
    search_planner = build_search_planner(
        raw, registry, prompts, retrieval_cfg=retrieval_cfg
    )
    reviewer_cfg = agents_cfg.get("reviewer", {})
    reviewer_model_invocation = reviewer_cfg.get("model_options")
    reviewer_model_options = (
        _model_options(reviewer_model_invocation)
        if isinstance(reviewer_model_invocation, Mapping)
        else None
    )
    workflow_cfg = raw.get("workflow", {})
    researcher_runtime = raw.get("researcher_runtime", {})
    runtime_tools = researcher_runtime.get("tools", {})
    web_cfg = runtime_tools.get("web_search", {})
    browser_cfg = runtime_tools.get("browser", {})
    reader_cfg = runtime_tools.get("reader", {})
    resolved_output_root = Path(
        output_root
        if output_root is not None
        else raw.get("runtime_debug", {}).get("output_root", "outputs")
    )
    store = ReferenceStore(resolved_output_root)
    reviewer = (
        NoveltyEvidenceReviewer(
            prompts=prompts,
            models=registry,
            config=EvidenceReviewerConfig(
                enabled=True,
                model_alias=str(reviewer_cfg.get("model", "reviewer")),
                temperature=float(reviewer_cfg.get("temperature", 0.0)),
                max_cards_per_call=int(reviewer_cfg.get("max_cards_per_call", 8)),
                fail_closed=bool(reviewer_cfg.get("fail_closed", True)),
                card_timeout_seconds=float(reviewer_cfg.get("card_timeout_seconds", 240)),
                summary_timeout_seconds=float(reviewer_cfg.get("summary_timeout_seconds", 180)),
                max_steps=int(reviewer_cfg.get("max_steps", 14)),
                max_tool_calls=int(reviewer_cfg.get("max_tool_calls", 12)),
                max_total_read_chars=int(
                    reviewer_cfg.get("max_total_read_chars", 96_000)
                ),
                prompt_name=str(reviewer_cfg.get("prompt", "reviewer/review_evidence")),
            ),
            model_options=reviewer_model_options,
            tool_registry=ResearcherToolRegistry(
                [
                    ReviewerReaderTool(
                        ReferenceArtifactReaderTool(
                            store,
                            max_chars_per_read=int(
                                reader_cfg.get("max_chars_per_read", 16_000)
                            ),
                        ),
                        default_chars_per_read=int(
                            reader_cfg.get("default_chars_per_read", 8_000)
                        ),
                    )
                ]
            ),
        )
        if reviewer_cfg.get("enabled", False)
        else None
    )
    researcher_tools: list[Any] = [
        ReferenceSearchTool(SubjectReferenceStore(resolved_output_root)),
        build_database_search_tool(
            retrieval_cfg,
            reference_store=store,
            source_registry=source_registry,
            max_concurrency=int(retrieval_cfg["max_concurrency"]),
            include_testing_only=False,
        ),
    ]
    if web_cfg.get("enabled", True):
        researcher_tools.append(
            WebSearchTool(
                BaiduSearchBackend(
                    timeout_seconds=float(
                        web_cfg.get("baidu", {}).get("timeout_seconds", 30.0)
                    )
                ),
                store,
                default_max_results=int(web_cfg.get("default_max_results", 10)),
                max_results_per_call=int(web_cfg.get("max_results_per_call", 50)),
            )
        )
    if browser_cfg.get("enabled", True):
        researcher_tools.append(
            BrowserTool(
                PlaywrightBrowserBackend(
                    network_mode=str(browser_cfg.get("network_mode", "inherit")),
                    navigation_timeout_ms=int(
                        browser_cfg.get("navigation_timeout_ms", 30_000)
                    ),
                    max_html_chars=int(browser_cfg.get("max_html_chars", 2_000_000)),
                    max_text_chars=int(browser_cfg.get("max_text_chars", 500_000)),
                ),
                store,
            )
        )
    researcher_tools.append(
        ReaderTool(
            ReferenceArtifactReaderTool(
                store,
                max_chars_per_read=int(reader_cfg.get("max_chars_per_read", 16_000)),
            ),
            default_chars_per_read=int(
                reader_cfg.get("default_chars_per_read", 8_000)
            ),
        )
    )
    tool_registry = ResearcherToolRegistry(researcher_tools)
    budget_cfg = raw.get("task_researcher", {})
    task_researcher = TaskResearcherWorkflow(
        research_model,
        tool_registry,
        EvidenceCardBuilder(store),
        prompts=prompts,
        config=TaskResearcherConfig(
            runtime_state_projection=bool(budget_cfg.get("runtime_state_projection", False)),
            guided_reading=bool(budget_cfg.get("guided_reading", False)),
            text_choices=bool(budget_cfg.get("text_choices", False)),
            reuse_reader_results=bool(budget_cfg.get("reuse_reader_results", False)),
            enable_evidence_checkpoint=bool(budget_cfg.get("enable_evidence_checkpoint", False)),
            max_steps=int(budget_cfg.get("max_steps", 12)),
            max_tool_calls=int(budget_cfg.get("max_tool_calls", 10)),
            max_chars_per_read=int(budget_cfg.get("max_chars_per_read", 8_000)),
            max_total_read_chars=int(
                budget_cfg.get("max_total_read_chars", 48_000)
            ),
            per_tool_limits=dict(
                budget_cfg.get(
                    "per_tool_limits",
                    {
                        "database_search": 2,
                        "web_search": 3,
                        "browser": 3,
                        "reader": 8,
                    },
                )
            ),
            model_options=(
                _model_options(researcher_runtime.get("model", {}))
                if researcher_runtime
                else ModelCallOptions(temperature=0.0, tool_choice="auto")
            ),
            prompt_name=str(
                researcher_runtime.get("prompt", "research/native_tool_loop")
            ),
        ),
    )
    return NoveltyWorkflow(
        NoveltyWorkflowServices(
            coordinator=coordinator,
            task_researcher=task_researcher,
            search_planner=search_planner,
            point_extractor=point_extractor,
            reviewer=reviewer,
        ),
        config=NoveltyWorkflowConfig(
            max_rounds=int(workflow_cfg.get("max_rounds", 2)),
            recovery_provider_order=tuple(workflow_cfg.get("recovery_provider_order", [])),
            max_concurrency=int(workflow_cfg.get("max_concurrency", 4)),
            min_final_evidence_cards_per_point=int(
                workflow_cfg.get("min_final_evidence_cards_per_point", 1)
            ),
            candidate_limit_per_task=int(
                retrieval_cfg.get("candidate_limit_per_task", 8)
            ),
            runtime_debug=RuntimeDebugConfig(
                enabled=bool(raw.get("runtime_debug", {}).get("enabled", True)),
                output_root=resolved_output_root,
                archive_root=Path(
                    raw.get("runtime_debug", {}).get(
                        "archive_root", "docs/experiments/runtime"
                    )
                ),
                max_inline_bytes=int(
                    raw.get("runtime_debug", {}).get("max_inline_bytes", 256_000)
                ),
                max_physical_provider_requests=int(
                    raw.get("runtime_debug", {}).get("max_physical_provider_requests", 48)
                ),
                max_model_calls=int(raw.get("runtime_debug", {}).get("max_model_calls", 80)),
                llm_pricing_path=Path(
                    raw.get("runtime_debug", {}).get("llm_pricing_path")
                    or RuntimeDebugConfig().llm_pricing_path
                ),
            ),
        ),
        runtime_config=_runtime_config_with_output_root(raw, resolved_output_root),
        output_root=resolved_output_root,
    )


class ReviewerRequiredError(RuntimeError):
    """标准完整工作流缺少必需 Reviewer 时的配置错误。"""

    code = "reviewer_required"


def build_standard_full_workflow(
    config: ApplicationConfig,
    *,
    source_registry: RetrievalSourceRegistry | None = None,
    output_root: str | Path | None = None,
) -> NoveltyWorkflow:
    """构造标准 Full/PaperInput workflow，并强制 Reviewer 装配契约。"""

    if config.reviewer is None:
        raise ReviewerRequiredError(
            "reviewer_required: Standard full workflow requires Reviewer, "
            "but reviewer config is missing"
        )
    if not config.reviewer.enabled:
        raise ReviewerRequiredError(
            "reviewer_required: Standard full workflow requires Reviewer, "
            "but reviewer.enabled=false"
        )
    from .experiment import prepare_startup
    resolved_root = Path(output_root if output_root is not None else config.project.runtime_debug.output_root)
    prepare_startup(config, output_root=resolved_root, entrypoint="standard_full_workflow")
    workflow = build_workflow(
        config, source_registry=source_registry, output_root=output_root
    )
    if workflow.services.reviewer is None:
        raise ReviewerRequiredError(
            "reviewer_required: Standard full workflow requires Reviewer, "
            "but Reviewer service construction returned unavailable"
        )
    return workflow


def _build_workflow_from_application_config(
    config: ApplicationConfig,
    *,
    source_registry: RetrievalSourceRegistry | None = None,
    output_root: str | Path | None = None,
) -> NoveltyWorkflow:
    """Build the production workflow directly from validated typed fields."""

    from .experiment import freeze_config

    ApplicationConfig.model_validate(config.model_dump(mode="json"))
    registry = build_model_registry(config)
    snapshot = config._startup_manifest.get("code", {}).get("source_snapshot", {})
    prompt_root = (Path(snapshot["path"]) / "source" / PROMPTS_ROOT.relative_to(Path(__file__).resolve().parents[4])
                   if snapshot else None)
    prompts = build_prompt_library(prompt_root)
    coordinator = NoveltyCoordinatorAgent(
        prompts=prompts,
        models=registry,
        model_alias=config.coordinator.model.alias,
        supplement_prompt_name=config.coordinator.prompt_names.supplement,
        synthesis_prompt_name=config.coordinator.prompt_names.synthesize,
        temperature=config.coordinator.model.temperature,
        model_options=_typed_model_options(
            config.coordinator.model,
            response_format={"type": "json_object"},
        ),
        research_languages=config.project.workflow.research_languages,
    )
    point_extractor = NoveltyPointExtractorAgent(
        prompts=prompts,
        models=registry,
        model_alias=config.point_extractor.model.alias,
        conservative_dedup=config.point_extractor.conservative_dedup,
        generation_prompt_name=config.point_extractor.prompt_names.generate,
        review_prompt_name=config.point_extractor.prompt_names.deduplicate,
        coverage_prompt_name=config.point_extractor.prompt_names.coverage,
        temperature=config.point_extractor.model.temperature,
        model_options=_typed_model_options(
            config.point_extractor.model,
            response_format={"type": "json_object"},
        ),
    )
    search_planner = SearchPlannerAgent(
        prompts=prompts,
        models=registry,
        model_alias=config.search_planner.model.alias,
        temperature=config.search_planner.model.temperature,
        model_options=_typed_model_options(
            config.search_planner.model,
            response_format={"type": "json_object"},
        ),
        max_attempts=config.search_planner.max_attempts,
        semantic_limits=SemanticLimits(
            **config.search_planner.limits.model_dump()
        ),
        prompt_name=config.search_planner.prompt,
    )
    database = config.researcher.tools.database_search
    retrieval = {
        "active_source": database.active_source,
        "candidate_limit_per_task": database.candidate_limit_per_task,
        "per_query_limit": database.per_query_limit,
        "max_provider_requests": database.max_provider_requests,
        "candidate_excerpt_chars": database.candidate_excerpt_chars,
        "full_text_limit_per_task": database.full_text_limit_per_task,
        "max_concurrency": database.max_concurrency,
        "sources": database.providers,
    }
    web = config.researcher.tools.web_search
    browser = config.researcher.tools.browser
    reader = config.researcher.tools.reader
    resolved_output_root = Path(
        output_root
        if output_root is not None
        else config.project.runtime_debug.output_root
    )
    store = ReferenceStore(resolved_output_root)
    reviewer = (
        NoveltyEvidenceReviewer(
            prompts=prompts,
            models=registry,
            config=EvidenceReviewerConfig(
                enabled=True,
                model_alias=config.reviewer.model.alias,
                temperature=config.reviewer.model.temperature,
                max_cards_per_call=config.reviewer.max_cards_per_call,
                fail_closed=config.reviewer.fail_closed,
                card_timeout_seconds=config.reviewer.card_timeout_seconds,
                summary_timeout_seconds=config.reviewer.summary_timeout_seconds,
                max_steps=config.reviewer.max_steps,
                max_tool_calls=config.reviewer.max_tool_calls,
                max_total_read_chars=config.reviewer.max_total_read_chars,
                prompt_name=config.reviewer.prompt,
            ),
            model_options=_typed_model_options(config.reviewer.model),
            tool_registry=ResearcherToolRegistry(
                [
                    ReviewerReaderTool(
                        ReferenceArtifactReaderTool(
                            store, max_chars_per_read=reader.max_chars_per_read
                        ),
                        default_chars_per_read=reader.default_chars_per_read,
                    )
                ]
            ),
        )
        if config.reviewer is not None and config.reviewer.enabled
        else None
    )
    researcher_tools: list[Any] = [
        ReferenceSearchTool(SubjectReferenceStore(resolved_output_root)),
        build_database_search_tool(
            retrieval,
            reference_store=store,
            source_registry=source_registry,
            max_concurrency=database.max_concurrency,
            include_testing_only=False,
        ),
    ]
    if web.enabled:
        researcher_tools.append(
            WebSearchTool(
                BaiduSearchBackend(
                    timeout_seconds=float(web.baidu.get("timeout_seconds", 30.0))
                ),
                store,
                default_max_results=web.default_max_results,
                max_results_per_call=web.max_results_per_call,
            )
        )
    if browser.enabled:
        researcher_tools.append(
            BrowserTool(
                PlaywrightBrowserBackend(
                    network_mode=browser.network_mode,
                    navigation_timeout_ms=browser.navigation_timeout_ms,
                    max_html_chars=browser.max_html_chars,
                    max_text_chars=browser.max_text_chars,
                ),
                store,
            )
        )
    researcher_tools.append(
        ReaderTool(
            ReferenceArtifactReaderTool(
                store, max_chars_per_read=reader.max_chars_per_read
            ),
            default_chars_per_read=reader.default_chars_per_read,
        )
    )
    tool_registry = ResearcherToolRegistry(researcher_tools)
    researcher = config.researcher
    task_researcher = TaskResearcherWorkflow(
        registry.client_for(researcher.model.alias),
        tool_registry,
        EvidenceCardBuilder(store),
        prompts=prompts,
        config=TaskResearcherConfig(
            runtime_state_projection=researcher.harness.runtime_state_projection,
            guided_reading=researcher.harness.guided_reading,
            text_choices=researcher.harness.text_choices,
            reuse_reader_results=researcher.harness.reuse_reader_results,
            enable_evidence_checkpoint=researcher.harness.enable_evidence_checkpoint,
            max_steps=researcher.harness.max_turns,
            max_tool_calls=researcher.harness.max_total_tool_calls,
            max_chars_per_read=reader.max_chars_per_read,
            max_total_read_chars=reader.max_total_read_chars,
            per_tool_limits=dict(researcher.harness.per_tool_limits),
            model_options=_typed_model_options(researcher.model),
            prompt_name=researcher.prompt,
        ),
    )
    workflow = config.project.workflow
    runtime_debug = config.project.runtime_debug
    frozen = copy.deepcopy(config._startup_manifest) if config._startup_manifest else freeze_config(config, output_root=resolved_output_root)
    runtime_snapshot = dict(frozen.pop("effective_config"))
    runtime_snapshot["configuration_manifest"] = frozen
    return NoveltyWorkflow(
        NoveltyWorkflowServices(
            coordinator=coordinator,
            task_researcher=task_researcher,
            search_planner=search_planner,
            point_extractor=point_extractor,
            reviewer=reviewer,
        ),
        config=NoveltyWorkflowConfig(
            max_rounds=workflow.max_rounds,
            recovery_provider_order=tuple(workflow.recovery_provider_order),
            max_concurrency=workflow.max_concurrency,
            min_final_evidence_cards_per_point=(
                workflow.min_final_evidence_cards_per_point
            ),
            candidate_limit_per_task=database.candidate_limit_per_task,
            runtime_debug=RuntimeDebugConfig(
                enabled=runtime_debug.enabled,
                output_root=resolved_output_root,
                archive_root=Path(runtime_debug.archive_root),
                max_inline_bytes=runtime_debug.max_inline_bytes,
                max_physical_provider_requests=runtime_debug.max_physical_provider_requests,
                max_model_calls=runtime_debug.max_model_calls,
                llm_pricing_path=(
                    Path(runtime_debug.llm_pricing_path)
                    if runtime_debug.llm_pricing_path
                    else RuntimeDebugConfig().llm_pricing_path
                ),
            ),
        ),
        runtime_config=_runtime_config_with_output_root(
            runtime_snapshot, resolved_output_root
        ),
        output_root=resolved_output_root,
    )


def _runtime_config_with_output_root(
    config: Mapping[str, Any], output_root: Path
) -> dict[str, Any]:
    """Keep Runtime Debug's captured config aligned with its physical workspace."""

    payload = copy.deepcopy(dict(config))
    if isinstance(payload.get("project"), dict):
        payload["project"].setdefault("runtime_debug", {})["output_root"] = str(
            output_root
        )
    else:
        payload.setdefault("runtime_debug", {})["output_root"] = str(output_root)
    return payload


def _normalized_retrieval_config(config: Mapping[str, Any]) -> dict[str, Any]:
    """读取新配置，并把历史 ``tools.arxiv`` 形状映射到通用结构。"""

    if "retrieval" in config:
        retrieval = copy.deepcopy(dict(config.get("retrieval", {})))
        # 过渡期允许旧开关显式启用 arXiv，避免既有部署静默失效。
        legacy_arxiv = config.get("tools", {}).get("arxiv", {})
        if legacy_arxiv.get("enabled"):
            retrieval.setdefault("sources", {}).setdefault("arxiv", {}).update(
                legacy_arxiv
            )
        retrieval.setdefault(
            "max_concurrency",
            int(config.get("workflow", {}).get("max_concurrency", 4)),
        )
        _inherit_arxiv_transport(retrieval, config)
        _adapt_legacy_arxiv_provider(retrieval)
        return retrieval
    arxiv = dict(config.get("tools", {}).get("arxiv", {}))
    # 旧配置没有来源选择语义：保留“Adapter 可用、网络工具关闭”的兼容行为。
    arxiv["adapter_only"] = not arxiv.get("enabled", False)
    arxiv["enabled"] = True
    retrieval = {
        "active_source": "arxiv",
        "candidate_limit_per_task": arxiv.get("candidate_limit", 8),
        "candidate_excerpt_chars": arxiv.get("candidate_excerpt_chars", 2000),
        "full_text_limit_per_task": arxiv.get("full_text_limit", 8),
        "max_concurrency": int(config.get("workflow", {}).get("max_concurrency", 4)),
        "sources": {"arxiv": arxiv},
    }
    _inherit_arxiv_transport(retrieval, config)
    _adapt_legacy_arxiv_provider(retrieval)
    return retrieval


#: 从 ``researcher.tools.database_search.providers.arxiv`` 继承到检索来源的键。
#: 生产路径（typed config）直接用 provider 段构造来源，而这条 legacy 路径过去
#: 只认 ``tools.arxiv`` / ``retrieval.sources.arxiv``，导致写在 provider 段里的
#: 参数对工作流检索静默无效。这里把通道选择相关键继承过来，让同一个开关同时
#: 覆盖「工作流检索」与「参考文献 bootstrap」两条链路。
_INHERITED_ARXIV_KEYS = (
    "search_transport",
    "web_min_interval_seconds",
    "web_timeout_seconds",
    "web_max_retries",
    "web_max_consecutive_failures",
    "web_page_size",
    "web_circuit_cooldown_seconds",
    "web_retry_budget_seconds",
    "web_max_retry_delay_seconds",
)


def _inherit_arxiv_transport(
    retrieval: dict[str, Any], config: Mapping[str, Any]
) -> None:
    """把 provider 段的检索通道参数并入来源配置；来源里已显式设置的键优先。"""

    provider = (
        config.get("researcher", {})
        .get("tools", {})
        .get("database_search", {})
        .get("providers", {})
        .get("arxiv")
    )
    source = retrieval.get("sources", {}).get("arxiv")
    if not isinstance(provider, Mapping) or not isinstance(source, dict):
        return
    for key in _INHERITED_ARXIV_KEYS:
        if key in provider and key not in source:
            source[key] = provider[key]


def _adapt_legacy_arxiv_provider(retrieval: dict[str, Any]) -> None:
    """Compatibility-only key adaptation; split config already uses canonical keys."""

    arxiv = retrieval.get("sources", {}).get("arxiv")
    if not isinstance(arxiv, dict):
        return
    arxiv.setdefault("search_transport", "web")
    arxiv.setdefault("min_interval_seconds", arxiv.pop("min_interval", 4.0))
    arxiv.setdefault("api_min_interval_seconds", arxiv["min_interval_seconds"])
    arxiv.setdefault("timeout_seconds", arxiv.pop("timeout", 20.0))
    arxiv.setdefault("max_retries", 1)
    arxiv.setdefault("max_retry_delay_seconds", 5.0)
    arxiv.setdefault("retry_budget_seconds", 45.0)
    arxiv.setdefault("circuit_failure_threshold", 2)
    arxiv.setdefault("circuit_cooldown_seconds", 60.0)
    arxiv.setdefault("full_text_max_chars", 100_000)
    arxiv.setdefault("scheduler_enabled", True)
    arxiv.setdefault("metadata_batch_enabled", True)
    arxiv.setdefault("metadata_batch_window_ms", 200)
    arxiv.setdefault("metadata_batch_max_size", 32)


def _apply_env_overrides(config: dict[str, Any]) -> None:
    """用 NOVELTY_<角色>_MODEL 覆盖 agents 里的模型别名。"""

    for role, agent_cfg in config.get("agents", {}).items():
        env_value = os.getenv(f"NOVELTY_{role.upper()}_MODEL")
        if env_value:
            agent_cfg["model"] = env_value
            config.setdefault("configuration_resolution", {}).setdefault("field_sources", {})[f"agents.{role}.model"] = "environment"


def _model_options(
    config: Mapping[str, Any],
    *,
    response_format: Mapping[str, Any] | None = None,
) -> ModelCallOptions:
    extra_body = {
        key: config[key]
        for key in ("enable_thinking", "thinking_budget", "reasoning_effort")
        if config.get(key) is not None
    }
    return ModelCallOptions(
        temperature=float(config["temperature"]),
        max_tokens=int(config["max_tokens"]),
        timeout_seconds=float(config["timeout_seconds"]),
        tool_choice=config.get("tool_choice"),
        response_format=response_format,
        extra_body=extra_body,
    )


def _typed_model_options(
    config: ModelInvocationConfig,
    *,
    response_format: Mapping[str, Any] | None = None,
) -> ModelCallOptions:
    extra_body = {
        key: value
        for key, value in {
            "enable_thinking": config.enable_thinking,
            "thinking_budget": config.thinking_budget,
            "reasoning_effort": config.reasoning_effort,
        }.items()
        if value is not None
    }
    return ModelCallOptions(
        temperature=config.temperature,
        max_tokens=config.max_tokens,
        timeout_seconds=config.timeout_seconds,
        tool_choice=config.tool_choice,
        response_format=response_format,
        extra_body=extra_body,
    )
