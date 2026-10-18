import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from backend.env import ChatMessage, ModelCallOptions
from novelty_agent_framework.config import (
    build_model_registry,
    build_workflow,
    load_application_config,
)
from novelty_agent_framework.config.loader import (
    DEFAULT_MODELS_PATH,
    DEFAULT_PROJECT_PATH,
    DEFAULT_REVIEWER_PATH,
    DEFAULT_RESEARCHER_PATH,
    DEFAULT_SEARCH_PLANNER_PATH,
)


def test_split_files_load_and_project_settings_are_slim():
    config = load_application_config(environ={})
    project = json.loads(DEFAULT_PROJECT_PATH.read_text(encoding="utf-8"))
    assert not {"models", "agents", "task_researcher", "retrieval"} & set(project)
    assert config.models and config.researcher.version == config.search_planner.version == 1
    assert config.researcher.model.alias == "local-qwen2.5-7b"
    assert config.search_planner.model.alias == "local-qwen2.5-7b"
    assert config.reviewer is not None
    assert config.reviewer.enabled is True


def test_example_files_contain_no_secret_values():
    combined = "\n".join(path.read_text(encoding="utf-8") for path in (
        DEFAULT_MODELS_PATH, DEFAULT_RESEARCHER_PATH, DEFAULT_SEARCH_PLANNER_PATH
    ))
    assert "api_key_env" in combined
    assert '"api_key"' not in combined
    assert "Bearer " not in combined


def test_invalid_numeric_value_fails_fast(tmp_path):
    raw = json.loads(DEFAULT_RESEARCHER_PATH.read_text(encoding="utf-8"))
    raw["harness"]["max_turns"] = 0
    path = tmp_path / "researcher.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ValidationError):
        load_application_config(researcher_path=path)


def test_reviewer_schema_defaults_to_enabled(tmp_path):
    raw = json.loads(DEFAULT_REVIEWER_PATH.read_text(encoding="utf-8"))
    raw.pop("enabled")
    path = tmp_path / "reviewer.json"
    path.write_text(json.dumps(raw), encoding="utf-8")

    config = load_application_config(reviewer_path=path)

    assert config.reviewer is not None
    assert config.reviewer.enabled is True


def test_unknown_model_alias_fails_fast(tmp_path):
    raw = json.loads(DEFAULT_RESEARCHER_PATH.read_text(encoding="utf-8"))
    raw["model"]["alias"] = "missing-model"
    path = tmp_path / "researcher.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ValidationError, match="unknown model aliases"):
        load_application_config(researcher_path=path, environ={})


def test_environment_model_override_is_loader_owned():
    config = load_application_config(environ={"NOVELTY_RESEARCH_MODEL": "glm4.7"})
    assert config.researcher.model.alias == "glm4.7"


def test_search_planner_uses_verified_default_without_environment_override():
    config = load_application_config(environ={})
    assert config.search_planner.model.alias == "local-qwen2.5-7b"


def test_default_text_llm_roles_are_unified_on_local_qwen():
    config = load_application_config(environ={})
    aliases = {
        config.coordinator.model.alias,
        config.point_extractor.model.alias,
        config.researcher.model.alias,
        config.search_planner.model.alias,
        config.reviewer.model.alias,
        config.project.processing["llm_model"],
    }

    assert aliases == {"local-qwen2.5-7b"}
    assert config.models["local-qwen2.5-7b"].model == "qwen2.5-7b-instruct"
    assert config.project.processing["ocr_model"] == "deepseek-ocr"


def test_official_deepseek_profile_maps_thinking_switch():
    config = load_application_config(environ={"DEEPSEEK_API_KEY": "test-key"})

    profile = config.models["deepseek-official-flash"]
    client = build_model_registry(config).client_for("deepseek-official-flash")
    payload = client._build_payload(
        [ChatMessage(role="user", content="hello")],
        ModelCallOptions(extra_body={"enable_thinking": False}),
    )

    assert profile.base_url == "https://api.deepseek.com"
    assert profile.model == "deepseek-flash"
    assert "enable_thinking" in profile.supported_params
    assert payload["thinking"] == {"type": "disabled"}
    assert "enable_thinking" not in payload


def test_processing_models_can_use_official_provider():
    config = load_application_config(environ={
        "NOVELTY_PROCESSING_LLM_MODEL": "deepseek-official-flash",
        "NOVELTY_PROCESSING_OCR_MODEL": "deepseek-official-flash",
    })
    assert config.project.processing["llm_model"] == "deepseek-official-flash"
    assert config.project.processing["ocr_model"] == "deepseek-official-flash"


def test_search_planner_example_filename_is_canonical():
    assert DEFAULT_SEARCH_PLANNER_PATH.name == "search_planner.example.json"
    assert DEFAULT_SEARCH_PLANNER_PATH.exists()
    assert not (DEFAULT_SEARCH_PLANNER_PATH.parent / "search_panner.example.json").exists()


def test_browser_network_mode_defaults_to_inherit(tmp_path: Path):
    raw = json.loads(DEFAULT_RESEARCHER_PATH.read_text(encoding="utf-8"))
    raw["tools"]["browser"].pop("network_mode", None)
    path = tmp_path / "researcher.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    config = load_application_config(researcher_path=path)
    assert config.researcher.tools.browser.network_mode == "inherit"


def test_browser_network_mode_rejects_invalid_value(tmp_path: Path):
    raw = json.loads(DEFAULT_RESEARCHER_PATH.read_text(encoding="utf-8"))
    raw["tools"]["browser"]["network_mode"] = "automatic"
    path = tmp_path / "researcher.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ValidationError):
        load_application_config(researcher_path=path)


def test_springer_provider_can_be_enabled_by_environment() -> None:
    config = load_application_config(
        environ={"NOVELTY_SPRINGER_ENABLED": "true"}
    )

    providers = config.researcher.tools.database_search.providers
    assert providers["arxiv"]["enabled"] is True
    assert providers["springer"]["enabled"] is True


def test_springer_provider_rejects_invalid_environment_switch() -> None:
    with pytest.raises(ValueError, match="NOVELTY_SPRINGER_ENABLED"):
        load_application_config(
            environ={"NOVELTY_SPRINGER_ENABLED": "sometimes"}
        )


@pytest.mark.parametrize(
    "mutate",
    [
        lambda raw: raw["tools"]["reader"].update(
            default_chars_per_read=1001, max_chars_per_read=1000
        ),
        lambda raw: raw["tools"]["reader"].update(max_chars_per_read=16001),
        lambda raw: raw["tools"]["web_search"].update(
            default_max_results=11, max_results_per_call=10
        ),
        lambda raw: raw["tools"]["web_search"].update(max_results_per_call=51),
    ],
)
def test_cross_field_limits_fail_fast(tmp_path: Path, mutate):
    raw = json.loads(DEFAULT_RESEARCHER_PATH.read_text(encoding="utf-8"))
    mutate(raw)
    path = tmp_path / "researcher.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ValidationError):
        load_application_config(researcher_path=path)


def test_typed_config_builds_default_tool_workflow_without_legacy_projection(
    monkeypatch,
):
    """默认配置只注册三个工具。

    web_search 与 browser 默认关闭：缺少凭据或浏览器运行库时它们必然失败，
    却照样消耗模型预算（实测每轮分别白扔近 40% 与 2 次调用）。
    """

    config = load_application_config()
    monkeypatch.setattr(
        "novelty_agent_framework.config.factory.legacy_shape",
        lambda _: (_ for _ in ()).throw(AssertionError("legacy_shape called")),
    )
    workflow = build_workflow(config)
    assert workflow.services.task_researcher.tools.names == (
        "reference_search", "database_search", "reader"
    )
    database = workflow.services.task_researcher.tools.get("database_search")
    planner = next(iter(database.tools_by_source.values())).search_planner
    assert planner is None
    assert (
        workflow.services.search_planner._model_alias
        == config.search_planner.model.alias
    )


def test_enabling_optional_tools_restores_them():
    config = load_application_config()
    config.researcher.tools.web_search.enabled = True
    config.researcher.tools.browser.enabled = True

    workflow = build_workflow(config)

    assert workflow.services.task_researcher.tools.names == (
        "reference_search", "database_search", "web_search", "browser", "reader"
    )
