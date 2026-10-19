"""Read, combine, and validate the split configuration files."""

from __future__ import annotations

import json
import copy
import hashlib
import os
from pathlib import Path
from typing import Any, Mapping

from .schemas import ApplicationConfig

CONFIG_DIR = Path(__file__).resolve().parent
DEFAULT_PROJECT_PATH = CONFIG_DIR / "settings.example.json"
DEFAULT_MODELS_PATH = CONFIG_DIR / "models.example.json"
DEFAULT_RESEARCHER_PATH = CONFIG_DIR / "agents" / "researcher.example.json"
DEFAULT_SEARCH_PLANNER_PATH = CONFIG_DIR / "agents" / "search_planner.example.json"
DEFAULT_COORDINATOR_PATH = CONFIG_DIR / "agents" / "coordinator.example.json"
DEFAULT_POINT_EXTRACTOR_PATH = CONFIG_DIR / "agents" / "point_extractor.example.json"
DEFAULT_REVIEWER_PATH = CONFIG_DIR / "agents" / "reviewer.example.json"


def read_json(path: str | Path) -> dict[str, Any]:
    resolved = Path(path)
    with resolved.open(encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise ValueError(f"configuration {resolved} must contain a JSON object")
    return payload


def load_application_config(
    *,
    project_path: str | Path = DEFAULT_PROJECT_PATH,
    models_path: str | Path = DEFAULT_MODELS_PATH,
    researcher_path: str | Path = DEFAULT_RESEARCHER_PATH,
    search_planner_path: str | Path = DEFAULT_SEARCH_PLANNER_PATH,
    coordinator_path: str | Path = DEFAULT_COORDINATOR_PATH,
    point_extractor_path: str | Path = DEFAULT_POINT_EXTRACTOR_PATH,
    reviewer_path: str | Path = DEFAULT_REVIEWER_PATH,
    environ: Mapping[str, str] | None = None,
    profile_path: str | Path | None = None,
    overrides: Mapping[str, Any] | None = None,
) -> ApplicationConfig:
    raw = {
        "project": read_json(project_path),
        "models": read_json(models_path),
        "researcher": read_json(researcher_path),
        "search_planner": read_json(search_planner_path),
        "coordinator": read_json(coordinator_path),
        "point_extractor": read_json(point_extractor_path),
        "reviewer": read_json(reviewer_path),
    }
    values = os.environ if environ is None else environ
    # All callers share: split files < profile < environment < explicit overrides.
    origins = {key: "split_files" for key in _leaf_paths(raw)}
    selected_profile = profile_path or values.get("NOVELTY_EXPERIMENT_PROFILE")
    if selected_profile:
        profile = read_json(selected_profile)
        _merge_config(raw, profile)
        origins.update({key: "profile" for key in _leaf_paths(profile)})
    before_env = copy.deepcopy(raw)
    _apply_model_overrides(raw, values)
    if values.get("NOVELTY_TIMEOUT_SECONDS"):
        for model in raw["models"].values():
            model["timeout_seconds"] = values["NOVELTY_TIMEOUT_SECONDS"]
    _apply_database_overrides(raw, values)
    old_leaves = _leaf_paths(before_env)
    origins.update({key: "environment" for key, value in _leaf_paths(raw).items()
                    if value != old_leaves.get(key)})
    for role, names in {
        "researcher": ("NOVELTY_RESEARCHER_MODEL", "NOVELTY_RESEARCH_MODEL"),
        "search_planner": ("NOVELTY_SEARCH_PLANNER_MODEL",),
        "coordinator": ("NOVELTY_COORDINATOR_MODEL",),
        "point_extractor": ("NOVELTY_POINT_EXTRACTOR_MODEL",),
        "reviewer": ("NOVELTY_REVIEWER_MODEL",),
    }.items():
        if any(values.get(name) for name in names):
            origins[f"{role}.model.alias"] = "environment"
    for field in ("llm_model", "ocr_model"):
        if values.get(f"NOVELTY_PROCESSING_{field.upper()}"):
            origins[f"project.processing.{field}"] = "environment"
    if values.get("NOVELTY_SPRINGER_ENABLED") is not None:
        origins["researcher.tools.database_search.providers.springer.enabled"] = "environment"
    if values.get("NOVELTY_TIMEOUT_SECONDS"):
        origins.update({f"models.{alias}.timeout_seconds": "environment" for alias in raw["models"]})
    if overrides:
        _merge_config(raw, overrides)
        origins.update({key: "explicit_override" for key in _leaf_paths(overrides)})
    config = ApplicationConfig.model_validate(raw)
    paths = dict(project=project_path, models=models_path, researcher=researcher_path,
                 search_planner=search_planner_path, coordinator=coordinator_path,
                 point_extractor=point_extractor_path, reviewer=reviewer_path)
    if selected_profile:
        paths["profile"] = selected_profile
    config._resolution = {
        "precedence": ["schema_defaults", "split_files", "profile", "environment", "explicit_override"],
        "sources": {name: {"path": str(Path(path).resolve()),
                           "sha256": hashlib.sha256(Path(path).read_bytes()).hexdigest()}
                    for name, path in paths.items()},
        "field_sources": {key: origins.get(key, "schema_default")
                          for key in _leaf_paths(config.model_dump(mode="json"))},
    }
    config._loaded_values = _leaf_paths(config.model_dump(mode="json"))
    return config


def _merge_config(target: dict[str, Any], overlay: Mapping[str, Any]) -> None:
    """Merge objects, replace arrays/scalars; typed schema rejects unknown keys."""
    for key, value in overlay.items():
        if isinstance(value, Mapping) and isinstance(target.get(key), dict):
            _merge_config(target[key], value)
        else:
            target[key] = copy.deepcopy(value)


def _leaf_paths(value: Mapping[str, Any], prefix: str = "") -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, item in value.items():
        path = f"{prefix}.{key}" if prefix else key
        if isinstance(item, Mapping) and item:
            result.update(_leaf_paths(item, path))
        else:
            result[path] = item
    return result


def _apply_model_overrides(raw: dict[str, Any], environ: Mapping[str, str]) -> None:
    mapping = {
        "researcher": ("NOVELTY_RESEARCHER_MODEL", "NOVELTY_RESEARCH_MODEL"),
        "search_planner": ("NOVELTY_SEARCH_PLANNER_MODEL",),
        "coordinator": ("NOVELTY_COORDINATOR_MODEL",),
        "point_extractor": ("NOVELTY_POINT_EXTRACTOR_MODEL",),
        "reviewer": ("NOVELTY_REVIEWER_MODEL",),
    }
    for role, names in mapping.items():
        value = next((environ[name] for name in names if environ.get(name)), None)
        if value:
            raw[role]["model"]["alias"] = value
    for field, name in (
        ("llm_model", "NOVELTY_PROCESSING_LLM_MODEL"),
        ("ocr_model", "NOVELTY_PROCESSING_OCR_MODEL"),
    ):
        if environ.get(name):
            raw["project"]["processing"][field] = environ[name]


def _apply_database_overrides(
    raw: dict[str, Any], environ: Mapping[str, str]
) -> None:
    value = environ.get("NOVELTY_SPRINGER_ENABLED")
    if value is None:
        return
    normalized = value.strip().lower()
    if normalized not in {"true", "false", "1", "0", "yes", "no"}:
        raise ValueError("NOVELTY_SPRINGER_ENABLED must be a boolean")
    raw["researcher"]["tools"]["database_search"]["providers"]["springer"][
        "enabled"
    ] = normalized in {"true", "1", "yes"}


def legacy_shape(config: ApplicationConfig) -> dict[str, Any]:
    """Compatibility only: project typed config for unmigrated legacy callers."""

    db = config.researcher.tools.database_search
    return {
        **config.project.model_dump(mode="python"),
        "models": {
            alias: item.model_dump(mode="python")
            for alias, item in config.models.items()
        },
        "agents": {
            "research": {
                "model": config.researcher.model.alias,
                "temperature": config.researcher.model.temperature,
            },
            "search_planner": {
                "model": config.search_planner.model.alias,
                "temperature": config.search_planner.model.temperature,
            },
            "coordinator": {
                "model": config.coordinator.model.alias,
                "model_options": config.coordinator.model.model_dump(mode="python"),
                "prompt_names": config.coordinator.prompt_names.model_dump(mode="json"),
                "temperature": config.coordinator.model.temperature,
            },
            "point_extractor": {
                "model": config.point_extractor.model.alias,
                "model_options": config.point_extractor.model.model_dump(mode="python"),
                "conservative_dedup": config.point_extractor.conservative_dedup,
                "harness": config.point_extractor.harness.model_dump(mode="json"),
                "prompt_names": config.point_extractor.prompt_names.model_dump(mode="json"),
                "temperature": config.point_extractor.model.temperature,
            },
            "reviewer": {
                "enabled": bool(config.reviewer and config.reviewer.enabled),
                "model": (
                    config.reviewer.model.alias if config.reviewer else "reviewer"
                ),
                "model_options": (
                    config.reviewer.model.model_dump(mode="python")
                    if config.reviewer
                    else None
                ),
                "temperature": (
                    config.reviewer.model.temperature if config.reviewer else 0.0
                ),
                "prompt": (
                    config.reviewer.prompt
                    if config.reviewer
                    else "reviewer/review_evidence"
                ),
                "max_cards_per_call": (
                    config.reviewer.max_cards_per_call if config.reviewer else 8
                ),
                "fail_closed": (
                    config.reviewer.fail_closed if config.reviewer else True
                ),
                "max_steps": config.reviewer.max_steps if config.reviewer else 14,
                "card_timeout_seconds": config.reviewer.card_timeout_seconds if config.reviewer else 240,
                "summary_timeout_seconds": config.reviewer.summary_timeout_seconds if config.reviewer else 180,
                "max_tool_calls": (
                    config.reviewer.max_tool_calls if config.reviewer else 12
                ),
                "max_total_read_chars": (
                    config.reviewer.max_total_read_chars
                    if config.reviewer
                    else 96_000
                ),
            },
        },
        "task_researcher": {
            "guided_reading": config.researcher.harness.guided_reading,
            "text_choices": config.researcher.harness.text_choices,
            "runtime_state_projection": config.researcher.harness.runtime_state_projection,
            "reuse_reader_results": config.researcher.harness.reuse_reader_results,
            "enable_evidence_checkpoint": config.researcher.harness.enable_evidence_checkpoint,
            "max_steps": config.researcher.harness.max_turns,
            "max_tool_calls": config.researcher.harness.max_total_tool_calls,
            "max_chars_per_read": config.researcher.tools.reader.max_chars_per_read,
            "default_chars_per_read": (
                config.researcher.tools.reader.default_chars_per_read
            ),
            "max_total_read_chars": config.researcher.tools.reader.max_total_read_chars,
            "per_tool_limits": config.researcher.harness.per_tool_limits,
        },
        "retrieval": {
            "active_source": db.active_source,
            "candidate_limit_per_task": db.candidate_limit_per_task,
            "per_query_limit": db.per_query_limit,
            "max_provider_requests": db.max_provider_requests,
            "candidate_excerpt_chars": db.candidate_excerpt_chars,
            "full_text_limit_per_task": db.full_text_limit_per_task,
            "max_concurrency": db.max_concurrency,
            "sources": db.providers,
        },
        "configuration_resolution": copy.deepcopy(config._resolution),
        "researcher_runtime": config.researcher.model_dump(mode="python"),
        "search_planner_runtime": config.search_planner.model_dump(mode="python"),
    }


def effective_safe_config(config: ApplicationConfig) -> dict[str, Any]:
    """Reproducible runtime view without API keys or environment values."""

    from .experiment import redact_config

    return redact_config({
        "workflow": config.project.workflow.model_dump(mode="json"),
        "runtime_debug": config.project.runtime_debug.model_dump(mode="json"),
        "processing": config.project.processing,
        "coordinator": config.coordinator.model_dump(mode="json"),
        "point_extractor": config.point_extractor.model_dump(mode="json"),
        "researcher": config.researcher.model_dump(mode="json"),
        "search_planner": config.search_planner.model_dump(mode="json"),
        "reviewer": (
            config.reviewer.model_dump(mode="json") if config.reviewer else None
        ),
        "models": {
            alias: {
                "provider": profile.provider,
                "base_url": profile.base_url,
                "model": profile.model,
                "context_window": profile.context_window,
                "context_admission": profile.context_admission.model_dump(mode="json"),
                "supported_params": profile.supported_params,
                "timeout_seconds": profile.timeout_seconds,
                "capabilities": profile.capabilities.model_dump(mode="json"),
                "api_key_env": profile.api_key_env,
            }
            for alias, profile in config.models.items()
        },
    })
