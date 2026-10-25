"""Formal-entrypoint preflight and reconstructable source/config snapshots (offline)."""
import asyncio
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from novelty_agent_framework.config import build_model_registry, build_standard_full_workflow, build_workflow, load_application_config
from novelty_agent_framework.config.loader import legacy_shape
from novelty_agent_framework.config.experiment import (
    ConfigurationPreflightError, freeze_config, preflight_config, prepare_startup, verify_startup_snapshot,
)


@pytest.fixture
def config(monkeypatch):
    monkeypatch.setenv("LOCAL_VLLM_API_KEY", "synthetic-local")
    monkeypatch.delenv("NOVELTY_API_KEY", raising=False)
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    return load_application_config(environ={})


def test_timeout_resolution_precedence_and_isolation_reach_actual_clients(config, monkeypatch):
    monkeypatch.setenv("NOVELTY_TIMEOUT_SECONDS", "123")
    isolated = build_model_registry(config)._profiles["local-qwen2.5-7b"]
    assert isolated.defaults["timeout_seconds"] == 60
    resolved = load_application_config(environ={"NOVELTY_TIMEOUT_SECONDS": "123"}, overrides={
        "models": {"deepseek-flash": {"timeout_seconds": 17}}})
    monkeypatch.setenv("NOVELTY_TIMEOUT_SECONDS", "456")
    registry = build_model_registry(resolved)
    assert registry._profiles["local-qwen2.5-7b"].defaults["timeout_seconds"] == 123
    assert registry._profiles["deepseek-flash"].defaults["timeout_seconds"] == 17
    frozen = freeze_config(resolved)
    assert frozen["effective_config"]["models"]["local-qwen2.5-7b"]["timeout_seconds"] == 123
    assert frozen["resolution"]["field_sources"]["models.local-qwen2.5-7b.timeout_seconds"] == "environment"
    assert frozen["resolution"]["field_sources"]["models.deepseek-flash.timeout_seconds"] == "explicit_override"


@pytest.mark.parametrize("value", ["0", "-1", "invalid"])
def test_invalid_environment_timeout_fails_during_load(value):
    with pytest.raises(ValidationError):
        load_application_config(environ={"NOVELTY_TIMEOUT_SECONDS": value})


@pytest.mark.parametrize("field,value,code", [
    ("tool_calling", None, "model_capability_unknown"),
    ("tool_calling", False, "model_capability_unsupported"),
    ("json_object", None, "model_capability_unknown"),
    ("json_object", False, "model_capability_unsupported"),
])
def test_formal_factory_rejects_unknown_or_unsupported_capability_before_build(config, monkeypatch, tmp_path, field, value, code):
    setattr(config.models["local-qwen2.5-7b"].capabilities, field, value)
    monkeypatch.setattr("novelty_agent_framework.config.factory.build_workflow", lambda *a, **k: pytest.fail("client construction reached"))
    with pytest.raises(ConfigurationPreflightError, match=code):
        build_standard_full_workflow(config, output_root=tmp_path)
    assert not list(tmp_path.iterdir())


def test_processing_preflight_follows_enabled_fallback_and_checks_vision(config):
    values = {"LOCAL_VLLM_API_KEY": "synthetic-local"}
    assert not [i for i in preflight_config(config, environ=values) if i["severity"] == "error"]
    assert not config.project.processing["ocr_fallback_enabled"]
    assert not [i for i in preflight_config(config, environ=values, include_processing=True) if i["severity"] == "error"]
    config.project.processing["ocr_fallback_enabled"] = True
    issues = preflight_config(config, environ=values, include_processing=True)
    assert any(i["path"] == "models.deepseek-ocr.api_key_env" for i in issues)
    config.models["deepseek-ocr"].capabilities.vision = None
    config.project.processing["ocr_fallback_enabled"] = False
    assert not [i for i in preflight_config(config, environ=values, include_processing=True) if i["severity"] == "error"]
    config.project.processing["ocr_fallback_enabled"] = True
    assert any(i["code"] == "model_capability_unknown" and i["path"] == "project.processing.ocr_model"
               for i in preflight_config(config, environ={**values, "SILICONFLOW_API_KEY": "synthetic-ocr"}, include_processing=True))


def test_ocr_switch_rejects_truthy_string():
    with pytest.raises(ValidationError):
        load_application_config(environ={}, overrides={"project": {"processing": {"ocr_fallback_enabled": "false"}}})


def test_tool_choice_and_recovery_provider_failfast(config):
    config.researcher.model.tool_choice = "required"
    config.coordinator.model.tool_choice = "auto"
    config.project.workflow.recovery_provider_order = ["null_catalog", "not-registered"]
    errors = {i["code"] for i in preflight_config(config, environ={"LOCAL_VLLM_API_KEY": "local"})}
    assert {"unsupported_tool_choice", "incompatible_model_option", "recovery_provider_unavailable"} <= errors


def test_single_task_does_not_require_unused_json_capability(config):
    config.models["local-qwen2.5-7b"].capabilities.json_object = None
    assert not [i for i in preflight_config(config, environ={"LOCAL_VLLM_API_KEY": "local"},
                                          active_roles=("researcher",), require_reviewer=False) if i["severity"] == "error"]


def test_startup_archives_source_inputs_dependencies_and_redacted_config(config, tmp_path):
    config.researcher.tools.database_search.providers["arxiv"]["api_key"] = "SENTINEL-PRIVATE"
    source_input = tmp_path / "paper.json"
    source_input.write_text('{"paper_id":"snapshot-test"}')
    frozen = prepare_startup(config, output_root=tmp_path, entrypoint="offline-test", input_path=source_input,
                             snapshot_dir=tmp_path / "startup", input_contents={"task.json": b'{"task_id":"T-1"}'})
    directory = Path(frozen["code"]["source_snapshot"]["path"])
    assert verify_startup_snapshot(directory)
    files = frozen["code"]["source_snapshot"]["source_files"]
    assert "backend/env/model_client.py" in files and "scripts/run_full_workflow_live.py" in files
    assert "pyproject.toml" in files and "requirements.txt" in files
    assert "backend/src/novelty_agent_framework/templates/markdown/default.md" in files
    assert "backend/src/novelty_agent_framework/config/llm_pricing.json" in files
    assert all(".env" not in Path(p).name for p in files)
    assert "tokenizers" in frozen["environment"]["packages"]
    for operations in frozen["selected_prompts"].values():
        for selected in operations.values():
            relative = "backend/src/novelty_agent_framework/prompts/" + selected["name"] + ".md"
            assert selected["sha256"] == files[relative]
    assert (directory / "inputs/task.json").read_bytes() == b'{"task_id":"T-1"}'
    assert (directory / "inputs/primary-input.json").read_bytes() == source_input.read_bytes()
    assert "SENTINEL-PRIVATE" not in (directory / "manifest.json").read_text()
    assert "SENTINEL-PRIVATE" not in (directory / "effective-config.json").read_text()
    for name, digest in files.items():
        assert hashlib.sha256((directory / "source" / name).read_bytes()).hexdigest() == digest
    # Content survives later working-tree changes; detection checks saved bytes.
    (directory / "source/backend/env/model_client.py").write_text("tampered")
    with pytest.raises(ValueError, match="checksum mismatch"):
        verify_startup_snapshot(directory)


def test_startup_detects_config_mutation_and_never_overwrites(config, tmp_path):
    first = prepare_startup(config, output_root=tmp_path, entrypoint="first", snapshot_dir=tmp_path / "startup")
    assert prepare_startup(config, output_root=tmp_path, entrypoint="reuse") == first
    config.researcher.model.max_tokens += 1
    with pytest.raises(ValueError, match="configuration changed"):
        prepare_startup(config, output_root=tmp_path, entrypoint="changed")
    other = load_application_config(environ={})
    with pytest.raises(FileExistsError):
        prepare_startup(other, output_root=tmp_path, entrypoint="overwrite", snapshot_dir=tmp_path / "startup")


def test_typed_and_projected_legacy_invocations_match(config, tmp_path):
    config.coordinator.model.timeout_seconds = 71
    config.point_extractor.model.max_tokens = 1777
    config.point_extractor.harness.reading_calls = 5
    typed = build_workflow(config, output_root=tmp_path / "typed")
    legacy = build_workflow(legacy_shape(config), output_root=tmp_path / "legacy")
    assert typed.services.coordinator.model_options == legacy.services.coordinator.model_options
    assert typed.services.point_extractor.model_options == legacy.services.point_extractor.model_options
    assert legacy.services.point_extractor.harness_config.reading_calls == 5


def test_service_preflight_precedes_processor_construction(config, tmp_path, monkeypatch):
    from novelty_agent_framework.services import workflow_service as service
    config.models["local-qwen2.5-7b"].capabilities.tool_calling = None
    monkeypatch.setattr(service, "load_application_config", lambda: config)
    monkeypatch.setattr(service, "build_model_registry", lambda *a: pytest.fail("registry construction reached"))
    with pytest.raises(service.WorkflowConfigurationError, match="model_capability_unknown"):
        service.build_real_workflow_service(SimpleNamespace(runs_root=tmp_path, max_upload_bytes=1000))


def test_service_disables_unneeded_ocr_client(config, tmp_path, monkeypatch):
    from novelty_agent_framework.services import workflow_service as service
    config.project.processing["ocr_fallback_enabled"] = False
    config.models["deepseek-ocr"].capabilities.vision = None
    monkeypatch.delenv("SILICONFLOW_API_KEY", raising=False)
    monkeypatch.setattr(service, "load_application_config", lambda: config)
    result = service.build_real_workflow_service(SimpleNamespace(runs_root=tmp_path, max_upload_bytes=1000))
    assert result.processor.ocr_client is None


def test_pdf_service_constructs_workflow_before_processing(tmp_path):
    from novelty_agent_framework.services.workflow_service import NoveltyWorkflowService
    calls = []
    class Processor:
        def process(self, *args, **kwargs):
            calls.append("process")
            raise RuntimeError("offline stop")
    def factory(root):
        calls.append("preflight-and-snapshot")
        return object()
    service = NoveltyWorkflowService(workflow_factory=factory, processor=Processor(), runs_root=tmp_path)
    snapshot = service.create_run()
    asyncio.run(service.execute_file(snapshot.task_id, tmp_path / "paper.pdf"))
    assert calls == ["preflight-and-snapshot", "process"]


def test_saved_snapshot_imports_and_rebuilds_config_in_isolated_process(config, tmp_path):
    import os
    import subprocess
    import sys
    frozen = prepare_startup(config, output_root=tmp_path, entrypoint="reconstruct", snapshot_dir=tmp_path / "startup")
    snapshot = Path(frozen["code"]["source_snapshot"]["path"])
    script = '''import json, sys
from pathlib import Path
root = Path(sys.argv[1])
sys.path[:0] = [str(root / "source/backend/src"), str(root / "source")]
import novelty_agent_framework.config.schemas as schemas
from novelty_agent_framework.config.factory import build_model_registry, build_workflow
from novelty_agent_framework.config.loader import load_application_config
from novelty_agent_framework.config.experiment import verify_startup_snapshot
assert Path(schemas.__file__).is_relative_to(root)
assert verify_startup_snapshot(root)
config = schemas.ApplicationConfig.model_validate_json((root / "effective-config.json").read_text())
assert load_application_config(environ={}).model_dump() == config.model_dump()
registry = build_model_registry(config)
assert registry._profiles["local-qwen2.5-7b"].defaults["timeout_seconds"] == config.models["local-qwen2.5-7b"].timeout_seconds
workflow = build_workflow(config, output_root=root.parent / "rebuilt-output")
assert workflow.services.reviewer is not None
print(json.dumps({"snapshot_source_imported": True, "typed_config_and_workflow_rebuilt": True, "network_calls": 0}))
'''
    result = subprocess.run([sys.executable, "-c", script, str(snapshot)], cwd=tmp_path,
                            env={"PATH": os.environ.get("PATH", ""), "PYTHONDONTWRITEBYTECODE": "1"},
                            capture_output=True, text=True, check=True)
    assert json.loads(result.stdout)["typed_config_and_workflow_rebuilt"] is True
