import json
from dataclasses import fields
from types import SimpleNamespace

import pytest

from novelty_agent_framework.agents import (
    NoveltyCoordinatorAgent,
    NoveltyEvidenceReviewer,
    SearchPlannerAgent,
)
from novelty_agent_framework.tools import (
    BaiduSearchBackend, BrowserTool, EvidenceCardBuilder, PlaywrightBrowserBackend,
    WebSearchTool,
)
from novelty_agent_framework.tools.database_search import RetrievalSourceRegistry
from novelty_agent_framework.workflows import NoveltyWorkflowServices, TaskResearcherWorkflow
from novelty_agent_framework.config import (
    ReviewerRequiredError,
    build_model_registry,
    build_structured_source_retrieval_tool,
    build_standard_full_workflow,
    build_workflow,
    load_config,
)
from novelty_agent_framework.config.loader import (
    legacy_shape,
    load_application_config,
)
from novelty_agent_framework.config.schemas import WorkflowConfig

BASE_CONFIG = {
    "workflow": {
        "max_rounds": 3,
        "max_concurrency": 2,
        "min_final_evidence_cards_per_point": 2,
    },
    "models": {
        "m1": {
            "model": "model-a",
            "base_url": "https://a.test/v1",
            "api_key_env": "TEST_KEY_A",
            "supported_params": ["enable_thinking"],
        },
        "m2": {
            "model": "model-b",
            "base_url": "https://b.test/v1",
            "api_key_env": "TEST_KEY_B",
        },
    },
    "agents": {
        "coordinator": {"model": "m1", "temperature": 0.1},
        "research": {"model": "m2", "temperature": 0.5},
    },
}


def test_workflow_config_rejects_final_evidence_cut_below_one():
    with pytest.raises(ValueError, match="min_final_evidence_cards_per_point"):
        WorkflowConfig.model_validate(
            {
                "max_rounds": 1,
                "max_concurrency": 1,
                "min_final_evidence_cards_per_point": 0,
            }
        )


def test_load_config_default_contains_models_and_agents(monkeypatch):
    # Assert the checked-in defaults independently of backend/.env or a local profile.
    for name in ("NOVELTY_EXPERIMENT_PROFILE", "NOVELTY_COORDINATOR_MODEL",
                 "NOVELTY_RESEARCHER_MODEL", "NOVELTY_RESEARCH_MODEL",
                 "NOVELTY_SEARCH_PLANNER_MODEL"):
        monkeypatch.delenv(name, raising=False)
    config = load_config()
    assert "models" in config
    assert "agents" in config
    assert config["agents"]["coordinator"]["model"] == "local-qwen2.5-7b"
    assert config["agents"]["search_planner"]["model"] == "local-qwen2.5-7b"
    assert config["agents"]["research"]["model"] == "local-qwen2.5-7b"


def test_build_workflow_wires_role_models(monkeypatch):
    monkeypatch.setenv("TEST_KEY_A", "key-a")
    monkeypatch.setenv("TEST_KEY_B", "key-b")

    workflow = build_workflow(BASE_CONFIG)

    assert workflow.config.max_rounds == 3
    assert workflow.config.max_concurrency == 2
    assert workflow.config.min_final_evidence_cards_per_point == 2
    assert workflow.config.candidate_limit_per_task == 8

    coordinator = workflow.services.coordinator
    task_researcher = workflow.services.task_researcher
    assert isinstance(coordinator, NoveltyCoordinatorAgent)
    assert isinstance(task_researcher, TaskResearcherWorkflow)
    assert isinstance(task_researcher.evidence_builder, EvidenceCardBuilder)
    assert coordinator._client().profile.model == "model-a"
    assert task_researcher.model_client.profile.model == "model-b"
    assert coordinator.temperature == 0.1


def test_build_workflow_env_overrides_role_model(monkeypatch):
    monkeypatch.setenv("TEST_KEY_A", "key-a")
    monkeypatch.setenv("TEST_KEY_B", "key-b")
    monkeypatch.setenv("NOVELTY_COORDINATOR_MODEL", "m2")

    workflow = build_workflow(BASE_CONFIG)

    assert workflow.services.coordinator._client().profile.model == "model-b"


def test_typed_workflow_output_root_reaches_all_stores_and_runtime(tmp_path):
    output_root = tmp_path / "runs" / "0001"
    workflow = build_workflow(load_application_config(), output_root=output_root)
    researcher = workflow.services.task_researcher

    assert workflow.output_root == output_root
    assert workflow.config.runtime_debug.output_root == output_root
    assert workflow.runtime_config["project"]["runtime_debug"]["output_root"] == str(
        output_root
    )
    assert researcher.evidence_builder.reference_store.output_root == output_root
    assert researcher.tools.get("reference_search").store.output_root == output_root
    assert (
        researcher.tools.get("database_search").reference_store.output_root
        == output_root
    )


@pytest.mark.parametrize("legacy", [False, True])
def test_default_controlled_mechanisms_reach_both_factories(tmp_path, legacy):
    config = load_application_config(environ={})
    workflow = build_workflow(legacy_shape(config) if legacy else config, output_root=tmp_path)
    researcher = workflow.services.task_researcher

    assert researcher.config.guided_reading
    assert researcher.config.text_choices
    assert researcher.config.prompt_name == "research/choice_only"
    assert researcher.harness.action_policy_factory is not None
    assert researcher.harness.config.text_choices
    assert researcher.harness.config.round_budgets
    assert not researcher.config.enable_evidence_checkpoint
    assert workflow.point_extractor.harness_config.enabled


@pytest.mark.parametrize("legacy", [False, True])
def test_explicit_native_researcher_configuration_is_preserved(tmp_path, legacy):
    config = load_application_config(environ={}, overrides={
        "researcher": {
            "prompt": "research/native_tool_loop",
            "harness": {"guided_reading": False, "text_choices": False,
                        "enable_evidence_checkpoint": True},
        },
        "point_extractor": {"harness": {"enabled": False}},
    })
    workflow = build_workflow(legacy_shape(config) if legacy else config, output_root=tmp_path)
    researcher = workflow.services.task_researcher

    assert researcher.harness.action_policy_factory is None
    assert not researcher.harness.config.text_choices
    assert not researcher.harness.config.round_budgets
    assert researcher.config.enable_evidence_checkpoint
    assert researcher.config.prompt_name == "research/native_tool_loop"
    assert not workflow.point_extractor.harness_config.enabled


def test_reviewer_composition_root_respects_enabled_switch():
    enabled = load_application_config()
    reviewer = build_workflow(enabled).services.reviewer
    assert isinstance(reviewer, NoveltyEvidenceReviewer)
    assert reviewer.tools.names == ("reader",)

    disabled = enabled.model_copy(
        update={
            "reviewer": enabled.reviewer.model_copy(update={"enabled": False})
        }
    )
    assert build_workflow(disabled).services.reviewer is None


def test_standard_full_workflow_requires_enabled_reviewer(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCAL_VLLM_API_KEY", "offline-test")
    config = load_application_config(environ={})
    config.project.runtime_debug.output_root = str(tmp_path)
    assert build_standard_full_workflow(config).services.reviewer is not None

    disabled = config.model_copy(
        update={"reviewer": config.reviewer.model_copy(update={"enabled": False})}
    )
    with pytest.raises(
        ReviewerRequiredError, match=r"reviewer_required.*reviewer\.enabled=false"
    ):
        build_standard_full_workflow(disabled)

    missing = config.model_copy(update={"reviewer": None})
    with pytest.raises(ReviewerRequiredError, match=r"reviewer_required.*config is missing"):
        build_standard_full_workflow(missing)

    monkeypatch.setattr(
        "novelty_agent_framework.config.factory.build_workflow",
        lambda *_args, **_kwargs: SimpleNamespace(
            services=SimpleNamespace(reviewer=None)
        ),
    )
    with pytest.raises(
        ReviewerRequiredError, match=r"reviewer_required.*construction.*unavailable"
    ):
        build_standard_full_workflow(config)


def test_build_workflow_does_not_mutate_input_config(monkeypatch):
    monkeypatch.setenv("NOVELTY_COORDINATOR_MODEL", "m2")
    original = json.dumps(BASE_CONFIG, sort_keys=True)

    build_workflow(BASE_CONFIG)

    assert json.dumps(BASE_CONFIG, sort_keys=True) == original


def test_registry_resolves_api_key_env(monkeypatch):
    monkeypatch.setenv("TEST_KEY_A", "secret-a")

    registry = build_model_registry(BASE_CONFIG)
    client = registry.client_for("m1")

    assert client.profile.api_key == "secret-a"


def test_services_hide_retrieval_implementation_details():
    workflow = build_workflow(BASE_CONFIG)
    assert {item.name for item in fields(NoveltyWorkflowServices)} == {
        "coordinator",
        "task_researcher",
        "search_planner",
        "point_extractor",
        "validator",
        "reviewer",
    }
    assert workflow.services.task_researcher.tools.names == (
        "reference_search", "database_search", "web_search", "browser", "reader")


def test_build_workflow_formal_path_ignores_legacy_arxiv_tools():
    config = json.loads(json.dumps(BASE_CONFIG))
    config["tools"] = {
        "arxiv": {
            "enabled": True,
            "min_interval": 0.0,
            "timeout": 5.0,
            "candidate_limit": 5,
        }
    }

    workflow = build_workflow(config)

    assert workflow.services.task_researcher.tools.names == (
        "reference_search", "database_search", "web_search", "browser", "reader")
    assert workflow.config.candidate_limit_per_task == 5


def test_formal_tools_share_one_reference_store():
    researcher = build_workflow(BASE_CONFIG).services.task_researcher
    reference_search, database, web, browser, reader = [
        researcher.tools.get(name) for name in researcher.tools.names
    ]
    store = researcher.evidence_builder.reference_store
    assert isinstance(web, WebSearchTool) and isinstance(web.backend, BaiduSearchBackend)
    assert isinstance(browser, BrowserTool) and isinstance(browser.backend, PlaywrightBrowserBackend)
    assert (
        database.reference_store
        is web.reference_store
        is browser.reference_store
        is reader.reader.reference_store
        is store
    )


def test_registry_does_not_mask_builder_key_error():
    registry = RetrievalSourceRegistry()

    def broken_builder(config):
        raise KeyError("required_setting")

    registry.register("broken", broken_builder)
    with pytest.raises(KeyError, match="required_setting"):
        registry.build("broken", {})


def test_build_structured_retrieval_tool_does_not_modify_workflow():
    config = json.loads(json.dumps(BASE_CONFIG))
    config["retrieval"] = {
        "active_source": "null_catalog",
        "candidate_limit_per_task": 3,
        "sources": {"null_catalog": {"enabled": True}},
    }
    tool = build_structured_source_retrieval_tool(config)
    assert tool.name == "structured_source_retrieval"
    assert tool.source_id == "null_catalog"
    assert tool.candidate_limit == 3

def test_legacy_shape_reviewer_receives_model_options():
    config = load_application_config()
    enabled = config.model_copy(
        update={
            "reviewer": config.reviewer.model_copy(update={"enabled": True})
        }
    )
    workflow = build_workflow(legacy_shape(enabled))
    reviewer = workflow.services.reviewer
    assert isinstance(reviewer, NoveltyEvidenceReviewer)
    assert reviewer.model_options is not None
    assert reviewer.model_options.max_tokens == config.reviewer.model.max_tokens
    assert reviewer.model_options.timeout_seconds == config.reviewer.model.timeout_seconds
