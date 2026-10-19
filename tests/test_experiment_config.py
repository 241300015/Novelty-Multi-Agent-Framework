"""Configuration control, preflight and secret-free experiment provenance."""
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from novelty_agent_framework.config import build_workflow, effective_safe_config, load_application_config
from novelty_agent_framework.config.experiment import freeze_config, preflight_config


def profile(tmp_path, data):
    path = tmp_path / "profile.json"
    path.write_text(json.dumps(data))
    return path


def test_profile_environment_and_explicit_precedence(tmp_path):
    path = profile(tmp_path, {"researcher": {"model": {"alias": "glm4.7"}},
                              "project": {"workflow": {"max_rounds": 3}}})
    config = load_application_config(profile_path=path,
        environ={"NOVELTY_RESEARCHER_MODEL": "deepseek-flash"},
        overrides={"researcher": {"model": {"alias": "local-qwen2.5-7b"}}})
    assert config.researcher.model.alias == "local-qwen2.5-7b"
    assert config.project.workflow.max_rounds == 3
    assert config._resolution["field_sources"]["researcher.model.alias"] == "explicit_override"
    assert config._resolution["field_sources"]["project.workflow.max_rounds"] == "profile"


def test_environment_profile_reaches_existing_callers(tmp_path):
    path = profile(tmp_path, {"project": {"workflow": {"research_languages": ["zh"]}}})
    config = load_application_config(environ={"NOVELTY_EXPERIMENT_PROFILE": str(path)})
    assert config.project.workflow.research_languages == ["zh"]
    assert config.researcher.model.alias == "local-qwen2.5-7b"


def test_environment_same_value_still_has_environment_origin():
    config = load_application_config(environ={"NOVELTY_RESEARCHER_MODEL": "local-qwen2.5-7b"})
    assert config._resolution["field_sources"]["researcher.model.alias"] == "environment"


@pytest.mark.parametrize("overlay", [
    {"project": {"workflow": {"max_rounds": 0}}},
    {"researcher": {"harenss": {"max_turns": 3}}},
    {"researcher": {"model": {"alias": "unknown"}}},
])
def test_invalid_profile_or_cli_override_fails_before_build(overlay):
    with pytest.raises(ValidationError):
        load_application_config(environ={}, overrides=overlay)


def test_snapshot_masks_nested_secrets_urls_and_headers(tmp_path):
    config = load_application_config(environ={})
    provider = config.researcher.tools.database_search.providers["arxiv"]
    provider.update(api_key="sentinel-secret", headers={"Authorization": "Bearer sentinel-header"},
                    endpoint="https://user:sentinel-password@example.org/path?api_key=sentinel-query")
    config.models["local-qwen2.5-7b"].base_url = "http://user:sentinel-local@localhost:8000/v1"
    for snapshot in (effective_safe_config(config), freeze_config(config)):
        encoded = json.dumps(snapshot)
        assert "sentinel-" not in encoded
        assert "LOCAL_VLLM_API_KEY" in encoded
    assert freeze_config(config)["resolution"]["field_sources"]["models.local-qwen2.5-7b.base_url"] == "runtime_override"


def test_snapshot_freezes_input_prompt_code_and_effective_output_root(tmp_path):
    config = load_application_config(environ={})
    source = tmp_path / "input.json"
    source.write_text('{"paper_id":"audit"}')
    snapshot = freeze_config(config, entrypoint="single_task", input_path=source, output_root=tmp_path)
    assert snapshot["entrypoint"] == "single_task"
    assert len(snapshot["input"]["sha256"]) == 64
    assert snapshot["prompt_sha256"]["research/choice_only.md"]
    assert len(snapshot["code"]["python_sources_sha256"]) == 64
    assert snapshot["effective_config"]["project"]["runtime_debug"]["output_root"] == str(tmp_path)
    before = snapshot["effective_config_sha256"]
    config.researcher.harness.max_turns += 1
    assert freeze_config(config, output_root=tmp_path)["effective_config_sha256"] != before


def test_snapshot_revalidates_post_load_mutation():
    config = load_application_config(environ={})
    config.project.workflow.max_rounds = 0
    with pytest.raises(ValidationError):
        freeze_config(config)
    with pytest.raises(ValidationError):
        build_workflow(config)


def test_preflight_classifies_provider_context_model_and_reviewer_errors():
    config = load_application_config(environ={})
    config.reviewer.enabled = False
    config.researcher.tools.database_search.providers["not_registered"] = {"enabled": True}
    config.researcher.tools.database_search.providers["ieee_xplore"]["enabled"] = True
    config.researcher.model.max_tokens = 40000
    config.researcher.model.enable_thinking = True
    errors = {issue["code"] for issue in preflight_config(config, environ={}) if issue["severity"] == "error"}
    assert {"unknown_provider", "provider_credential_missing", "context_budget_conflict",
            "model_credential_missing", "reviewer_required", "unsupported_model_parameter"} <= errors


def test_local_preflight_has_no_errors_with_explicit_credential():
    config = load_application_config(environ={})
    issues = preflight_config(config, environ={"LOCAL_VLLM_API_KEY": "local"})
    assert not [i for i in issues if i["severity"] == "error"]
    assert {i["code"] for i in issues} >= {"context_input_not_preflighted"}


@pytest.mark.parametrize("flag", ["enabled", "testing_only"])
def test_preflight_rejects_truthy_false_provider_strings(flag):
    config = load_application_config(environ={}, overrides={
        "researcher": {"tools": {"database_search": {"providers": {"arxiv": {flag: "false"}}}}}})
    errors = {i["code"] for i in preflight_config(config, environ={"LOCAL_VLLM_API_KEY": "local"})}
    assert "invalid_provider_switch" in errors


def test_signed_url_and_new_runtime_field_are_safe_and_tracked():
    config = load_application_config(environ={})
    config.researcher.tools.database_search.providers['arxiv']['endpoint'] = 'https://example.test/?sig=SENTINEL_SAS'
    frozen = freeze_config(config)
    assert 'SENTINEL_SAS' not in json.dumps(frozen)
    assert frozen['resolution']['field_sources']['researcher.tools.database_search.providers.arxiv.endpoint'] == 'runtime_override'


def test_preflight_matches_provider_default_credentials_and_fulltext_mode():
    config = load_application_config(environ={})
    providers = config.researcher.tools.database_search.providers
    providers['ieee_xplore'].update(enabled=True)
    providers['ieee_xplore'].pop('api_key_env')
    providers['springer'].update(enabled=True, full_text_mode='openaccess')
    values = {'LOCAL_VLLM_API_KEY': 'local', 'IEEE_XPLORE_API_KEY': 'dummy-ieee',
              'SPRINGER_NATURE_META_API_KEY': 'dummy-meta'}
    errors = [i for i in preflight_config(config, environ=values) if i['severity'] == 'error']
    assert [i['path'] for i in errors] == ['providers.springer.open_access_api_key_env']
    values['SPRINGER_NATURE_OPEN_ACCESS_API_KEY'] = 'dummy-oa'
    assert not [i for i in preflight_config(config, environ=values) if i['severity'] == 'error']


def test_single_task_preflight_does_not_require_unused_role_credentials():
    config = load_application_config(environ={}, overrides={'coordinator': {'model': {'alias': 'glm4.7'}}})
    issues = preflight_config(config, require_reviewer=False, active_roles=('researcher',),
                              environ={'LOCAL_VLLM_API_KEY': 'local'})
    assert not [i for i in issues if i['severity'] == 'error']
