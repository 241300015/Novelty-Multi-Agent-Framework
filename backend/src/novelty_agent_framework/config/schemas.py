"""Typed configuration contracts; no runtime construction belongs here."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, model_validator


class ConfigModel(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)


class ContextAdmissionSettings(ConfigModel):
    mode: Literal["off", "observe", "enforce"] = "off"
    counter: Literal["none", "vllm"] = "none"
    vllm_tools_mode: Literal["native", "v0_8_5_kwargs"] = "native"
    on_unavailable: Literal["allow", "reject"] = "reject"
    timeout_seconds: float = Field(default=5.0, gt=0, le=60)
    tokenize_path: str = Field(default="/tokenize", min_length=1)

    @model_validator(mode="after")
    def same_origin_tokenize_path(self):
        from urllib.parse import urlsplit
        parsed = urlsplit(self.tokenize_path)
        if (not self.tokenize_path.startswith("/") or self.tokenize_path.startswith("//")
                or parsed.scheme or parsed.netloc or parsed.query or parsed.fragment):
            raise ValueError("tokenize_path must be a same-origin absolute path")
        return self


class ModelCapabilities(ConfigModel):
    """Explicit deployment declarations; null means unverified, never inferred."""

    tool_calling: bool | None = None
    json_object: bool | None = None
    vision: bool | None = None
    tool_choices: list[str] = Field(default_factory=list)
    evidence: dict[str, str] = Field(default_factory=dict)


class ModelProfileConfig(ConfigModel):
    provider: str
    base_url: str
    model: str
    api_key_env: str | None = None
    context_window: int = Field(gt=0)
    timeout_seconds: float = Field(default=60.0, gt=0)
    capabilities: ModelCapabilities = Field(default_factory=ModelCapabilities)
    supported_params: list[str] = Field(default_factory=list)
    context_admission: ContextAdmissionSettings = Field(default_factory=ContextAdmissionSettings)


class ModelInvocationConfig(ConfigModel):
    alias: str = Field(min_length=1)
    temperature: float = Field(ge=0.0, le=2.0)
    max_tokens: int = Field(gt=0)
    timeout_seconds: float = Field(gt=0)
    tool_choice: str | None = None
    enable_thinking: bool | None = None
    thinking_budget: int | None = Field(default=None, gt=0)
    reasoning_effort: str | None = None


class HarnessConfig(ConfigModel):
    guided_reading: bool = True
    text_choices: bool = True
    runtime_state_projection: bool = False
    enable_evidence_checkpoint: bool = False
    reuse_reader_results: bool = False
    max_turns: int = Field(gt=0)
    max_total_tool_calls: int = Field(gt=0)
    per_tool_limits: dict[str, int] = Field(description=(
        "Tool call quotas per research round when guided_reading is enabled; otherwise per task."))

    @model_validator(mode="after")
    def positive_limits(self):
        if self.text_choices and not self.guided_reading:
            raise ValueError("text_choices requires guided_reading=true")
        if self.guided_reading and self.enable_evidence_checkpoint:
            raise ValueError("guided_reading currently requires enable_evidence_checkpoint=false")
        if not self.per_tool_limits or any(
            value < 1 for value in self.per_tool_limits.values()
        ):
            raise ValueError("per_tool_limits must contain positive limits")
        return self


class DatabaseSearchConfig(ConfigModel):
    active_source: str = Field(min_length=1)
    candidate_limit_per_task: int = Field(gt=0, description=(
        "Legacy field name: candidate limit per discovery invocation, independently renewed each round."))
    per_query_limit: int | None = Field(default=None, gt=0)
    max_provider_requests: int = Field(default=6, gt=0)
    candidate_excerpt_chars: int = Field(gt=0)
    full_text_limit_per_task: int = Field(ge=0)
    max_concurrency: int = Field(gt=0)
    providers: dict[str, dict[str, Any]]


class WebSearchConfig(ConfigModel):
    backend: Literal["baidu"]
    # 关闭后该工具不再注册进 Researcher 工具表。缺少可用凭据时它每次调用都
    # 必然失败，却照样消耗模型预算（实测 22 次调用 0 成功，占近 40% 预算）。
    enabled: bool = True
    default_max_results: int = Field(gt=0)
    max_results_per_call: int = Field(gt=0)
    baidu: dict[str, Any]

    @model_validator(mode="after")
    def valid_result_limits(self):
        if self.default_max_results > self.max_results_per_call:
            raise ValueError(
                "default_max_results must not exceed max_results_per_call"
            )
        if self.max_results_per_call > 50:
            raise ValueError("max_results_per_call exceeds Baidu hard limit 50")
        return self


class BrowserConfig(ConfigModel):
    backend: Literal["playwright"]
    # 与 web_search 同理：运行环境未安装浏览器运行库时该工具必然失败，却照样
    # 消耗模型预算（实测每轮白扔 2 次调用）。需要时显式打开并安装 playwright。
    enabled: bool = True
    network_mode: Literal["inherit", "direct"] = "inherit"
    navigation_timeout_ms: int = Field(gt=0)
    max_html_chars: int = Field(gt=0)
    max_text_chars: int = Field(gt=0)


class ReaderConfig(ConfigModel):
    default_chars_per_read: int = Field(gt=0)
    max_chars_per_read: int = Field(gt=0)
    max_total_read_chars: int = Field(gt=0)

    @model_validator(mode="after")
    def valid_read_limits(self):
        if self.default_chars_per_read > self.max_chars_per_read:
            raise ValueError(
                "default_chars_per_read must not exceed max_chars_per_read"
            )
        if self.max_chars_per_read > 16_000:
            raise ValueError("max_chars_per_read exceeds ReaderArguments cap 16000")
        return self


class ResearcherToolsConfig(ConfigModel):
    database_search: DatabaseSearchConfig
    web_search: WebSearchConfig
    browser: BrowserConfig
    reader: ReaderConfig


class ResearcherConfig(ConfigModel):
    version: int = Field(ge=1)
    model: ModelInvocationConfig
    prompt: str = Field(min_length=1)
    harness: HarnessConfig
    tools: ResearcherToolsConfig


class SearchPlannerLimitsConfig(ConfigModel):
    """SearchPlanner 语义校验的数量预算（与 prompt 数字一致，配置可覆盖）。"""

    max_concepts: int = Field(default=6, gt=0)
    max_terms_per_concept: int = Field(default=5, gt=0)
    max_alias_per_concept: int = Field(default=4, ge=0)
    max_exclude_per_concept: int = Field(default=3, ge=0)
    max_term_words: int = Field(default=8, gt=0)
    require_escape: bool = False


class SearchPlannerConfig(ConfigModel):
    version: int = Field(ge=1)
    model: ModelInvocationConfig
    prompt: str = Field(min_length=1)
    max_attempts: int = Field(gt=0)
    limits: SearchPlannerLimitsConfig = Field(
        default_factory=SearchPlannerLimitsConfig
    )


class ReviewerConfig(ConfigModel):
    version: int = Field(ge=1)
    enabled: bool = True
    model: ModelInvocationConfig
    prompt: str = Field(min_length=1)
    max_cards_per_call: int = Field(gt=0)
    fail_closed: bool = True
    max_steps: int = Field(default=14, gt=0)
    max_tool_calls: int = Field(default=12, gt=0)
    max_total_read_chars: int = Field(default=96_000, gt=0)
    card_timeout_seconds: float = Field(default=240, gt=0)
    summary_timeout_seconds: float = Field(default=180, gt=0)


class RoleAgentConfig(ConfigModel):
    version: int = Field(ge=1)
    model: ModelInvocationConfig
    # Legacy inventory only. New selection is keyed by operation below.
    prompts: list[str] = Field(default_factory=list)


class CoordinatorPromptConfig(ConfigModel):
    supplement: str = Field(default="coordinator/supplement", min_length=1)
    synthesize: str = Field(default="coordinator/synthesize", min_length=1)


class PointExtractorPromptConfig(ConfigModel):
    read: str = Field(default="extractor/read_paper", min_length=1)
    generate: str = Field(default="extractor/extract_points", min_length=1)
    deduplicate: str = Field(default="reviewer/review_points", min_length=1)
    coverage: str = Field(default="extractor/extract_points", min_length=1)


class CoordinatorConfig(RoleAgentConfig):
    prompt_names: CoordinatorPromptConfig = Field(default_factory=CoordinatorPromptConfig)


class ExtractorControlConfig(ConfigModel):
    enabled: bool = True
    generation_calls: int = Field(default=3, ge=1)
    review_calls: int = Field(default=3, ge=1)
    reading_calls: int = Field(default=3, ge=1)
    max_read_segments: int = Field(default=24, ge=1)
    max_read_chars: int = Field(default=20000, ge=1)
    generate_prompt: str = "extractor/generate"
    review_prompt: str = "extractor/semantic_review"
    reader_prompt: str = "extractor/reader_choices"
    correction_prompt: str = "extractor/correct"


class PointExtractorConfig(RoleAgentConfig):
    conservative_dedup: bool = True
    harness: ExtractorControlConfig = Field(default_factory=ExtractorControlConfig)
    prompt_names: PointExtractorPromptConfig = Field(default_factory=PointExtractorPromptConfig)


class ServerConfig(ConfigModel):
    host: str
    port: int = Field(ge=1, le=65535)


class CorsConfig(ConfigModel):
    origins: list[str]


class WorkflowConfig(ConfigModel):
    recovery_provider_order: list[str] = Field(default_factory=list)
    max_rounds: int = Field(gt=0)
    max_concurrency: int = Field(gt=0)
    min_final_evidence_cards_per_point: int = Field(ge=1)
    research_languages: list[Literal["en", "zh"]] = Field(
        default_factory=lambda: ["en"], min_length=1
    )


class RuntimeDebugSettingsConfig(ConfigModel):
    enabled: bool = True
    output_root: str = "outputs"
    archive_root: str = "docs/experiments/runtime"
    max_inline_bytes: int = Field(default=256_000, gt=0)
    max_physical_provider_requests: int = Field(default=48, gt=0)
    max_model_calls: int = Field(default=80, gt=0)
    llm_pricing_path: str | None = None


class ProjectSettingsConfig(ConfigModel):
    server: ServerConfig
    cors: CorsConfig
    workflow: WorkflowConfig
    processing: dict[str, Any]
    runtime_debug: RuntimeDebugSettingsConfig = Field(
        default_factory=RuntimeDebugSettingsConfig
    )


    @model_validator(mode="after")
    def explicit_processing_switch(self):
        self.processing.setdefault("ocr_fallback_enabled", True)
        if type(self.processing["ocr_fallback_enabled"]) is not bool:
            raise ValueError("processing.ocr_fallback_enabled must be a JSON boolean")
        if self.processing["ocr_fallback_enabled"] and not self.processing.get("ocr_model"):
            # No OCR alias has always meant no OCR client; preserve that contract.
            self.processing["ocr_fallback_enabled"] = False
        return self


class ApplicationConfig(ConfigModel):
    _resolution: dict[str, Any] = PrivateAttr(default_factory=dict)
    _loaded_values: dict[str, Any] = PrivateAttr(default_factory=dict)
    _startup_manifest: dict[str, Any] = PrivateAttr(default_factory=dict)
    project: ProjectSettingsConfig
    models: dict[str, ModelProfileConfig]
    researcher: ResearcherConfig
    search_planner: SearchPlannerConfig
    reviewer: ReviewerConfig | None = None
    coordinator: CoordinatorConfig
    point_extractor: PointExtractorConfig

    @model_validator(mode="after")
    def known_model_aliases(self):
        aliases = set(self.models)
        roles = {
            "researcher": self.researcher.model.alias,
            "search_planner": self.search_planner.model.alias,
            "coordinator": self.coordinator.model.alias,
            "point_extractor": self.point_extractor.model.alias,
        }
        if self.reviewer is not None and self.reviewer.enabled:
            roles["reviewer"] = self.reviewer.model.alias
        unknown = {role: alias for role, alias in roles.items() if alias not in aliases}
        for key in ("ocr_model", "llm_model"):
            alias = self.project.processing.get(key)
            if alias and alias not in aliases:
                unknown[f"processing.{key}"] = alias
        if unknown:
            raise ValueError(f"unknown model aliases: {unknown}")
        return self
