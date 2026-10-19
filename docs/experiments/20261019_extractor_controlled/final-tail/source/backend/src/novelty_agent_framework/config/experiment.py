"""Experiment preflight and reproducible, secret-free configuration manifests."""
from __future__ import annotations

import hashlib
import copy
import json
import os
import platform
from importlib.metadata import PackageNotFoundError, distributions, version
import re
import subprocess
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .schemas import ApplicationConfig

PROMPTS_ROOT = Path(__file__).resolve().parents[1] / "prompts"
PROJECT_ROOT = Path(__file__).resolve().parents[4]


def _secret_key(key: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]", "", key.lower())
    if normalized.endswith("env"):
        return False
    return any(word in normalized for word in
               ("apikey", "credential", "password", "secret", "authorization", "cookie", "privatekey", "accesskey", "signature")) or normalized.endswith("token") or normalized in {
                   "key", "sig", "sas", "jwt", "token", "accesstoken", "refreshtoken", "institutiontoken", "signature"}


def redact_config(value: Any) -> Any:
    """Preserve credential *references*, never credential values or signed URLs."""
    if isinstance(value, Mapping):
        return {str(key): "[REDACTED]" if _secret_key(str(key)) else redact_config(item)
                for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact_config(item) for item in value]
    if isinstance(value, str):
        if value.startswith(("http://", "https://")):
            parts = urlsplit(value)
            netloc = parts.netloc.rsplit("@", 1)[-1]
            query = urlencode([(key, "[REDACTED]" if _secret_key(key) or key.lower() == "key" else item)
                               for key, item in parse_qsl(parts.query, keep_blank_values=True)])
            value = urlunsplit((parts.scheme, netloc, parts.path, query, parts.fragment))
        value = re.sub(r"(?i)\bBearer\s+[^\s\"']+", "Bearer [REDACTED]", value)
        value = re.sub(r"(?i)\b(api[_-]?key|token|secret|password)\s*[=:]\s*[^\s,;&]+",
                       r"\1=[REDACTED]", value)
    return value


def _sha(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":")).encode()).hexdigest()



def configured_prompt_names(config: ApplicationConfig) -> dict[str, dict[str, str]]:
    result = {
        "coordinator": config.coordinator.prompt_names.model_dump(),
        "point_extractor": config.point_extractor.prompt_names.model_dump(),
        "researcher": {"research": config.researcher.prompt},
        "search_planner": {"plan": config.search_planner.prompt},
    }
    if config.reviewer is not None and config.reviewer.enabled:
        result["reviewer"] = {"review": config.reviewer.prompt}
    if config.point_extractor.harness.enabled:
        harness = config.point_extractor.harness
        result["point_extractor"] = {
            "generate": harness.generate_prompt, "semantic_review": harness.review_prompt,
            "read": harness.reader_prompt, "correct": harness.correction_prompt,
        }
    return result


def _prompt_file(name: str) -> Path | None:
    candidate = (PROMPTS_ROOT / f"{name}.md").resolve()
    if not candidate.is_relative_to(PROMPTS_ROOT.resolve()):
        return None
    return candidate if candidate.is_file() else None


def _selected_prompt_manifest(config: ApplicationConfig) -> dict[str, Any]:
    from backend.env.prompt_library import parse_front_matter
    result = {}
    for role, operations in configured_prompt_names(config).items():
        result[role] = {}
        for operation, name in operations.items():
            path = _prompt_file(name)
            body = path.read_bytes() if path else None
            metadata = parse_front_matter(body.decode("utf-8"))[0] if body is not None else {}
            result[role][operation] = {
                "name": name, "version": metadata.get("version"),
                "sha256": hashlib.sha256(body).hexdigest() if body is not None else None,
                "exists": body is not None,
            }
    return result

def freeze_config(config: ApplicationConfig, *, entrypoint: str = "workflow",
                  input_path: Path | None = None, output_root: Path | None = None) -> dict[str, Any]:
    """Validate again after caller mutations, then snapshot actual run settings."""
    raw = config.model_dump(mode="json")
    if output_root is not None:
        raw["project"]["runtime_debug"]["output_root"] = str(output_root)
    effective = redact_config(ApplicationConfig.model_validate(raw).model_dump(mode="json"))
    from .loader import _leaf_paths
    resolution = copy.deepcopy(config._resolution)
    origins = resolution.setdefault("field_sources", {})
    for path, value in _leaf_paths(raw).items():
        if path not in config._loaded_values or value != config._loaded_values[path]:
            origins[path] = "runtime_override"
    if output_root is not None:
        origins["project.runtime_debug.output_root"] = "runtime_output_root"

    prompts = {p.relative_to(PROMPTS_ROOT).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
               for p in sorted(PROMPTS_ROOT.rglob("*.md"))}
    try:
        commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=PROJECT_ROOT,
                                         text=True, stderr=subprocess.DEVNULL).strip()
        dirty = bool(subprocess.check_output(["git", "status", "--porcelain"], cwd=PROJECT_ROOT,
                                            text=True, stderr=subprocess.DEVNULL).strip())
    except (OSError, subprocess.CalledProcessError):
        commit, dirty = None, None
    # Includes uncommitted code and config without recording environment files.
    source_files = [p for folder in (PROJECT_ROOT / "backend", PROJECT_ROOT / "scripts")
                    for p in folder.rglob("*.py") if "__pycache__" not in p.parts]
    source_hash = _sha({p.relative_to(PROJECT_ROOT).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                        for p in sorted(source_files)})
    packages = {}
    for name in ("pydantic", "langgraph", "httpx", "python-dotenv", "PyMuPDF",
                 "transformers", "tokenizers", "tiktoken", "vllm"):
        try:
            packages[name] = version(name)
        except PackageNotFoundError:
            packages[name] = None
    return {
        "schema_version": 1, "entrypoint": entrypoint,
        "environment": {"python": platform.python_version(), "platform": platform.platform(),
                        "packages": packages,
                        "installed_packages": dict(sorted((item.metadata["Name"], item.version)
                                                          for item in distributions() if item.metadata.get("Name")))},
        "effective_config": effective, "effective_config_sha256": _sha(effective),
        "resolution": redact_config(resolution),
        "selected_prompts": _selected_prompt_manifest(config),
        "prompt_sha256": prompts, "code": {"commit": commit, "dirty": dirty,
                                           "python_sources_sha256": source_hash},
        "input": ({"path": str(input_path.resolve()),
                   "sha256": hashlib.sha256(input_path.read_bytes()).hexdigest()}
                  if input_path else None),
        "secret_values_included": False,
    }


def preflight_config(config: ApplicationConfig, *, environ: Mapping[str, str] | None = None,
                     require_reviewer: bool = True,
                     active_roles: tuple[str, ...] | None = None,
                     include_processing: bool = False) -> list[dict[str, str]]:
    """Offline checks only. A present credential is not proof of authorization."""
    values = os.environ if environ is None else environ
    # Reject invalid assignments made after loading.
    ApplicationConfig.model_validate(config.model_dump(mode="json"))
    issues: list[dict[str, str]] = []

    def add(code: str, path: str, message: str, severity: str = "error") -> None:
        issues.append(dict(code=code, path=path, message=message, severity=severity))

    roles = {name: getattr(config, name) for name in
             ("researcher", "search_planner", "coordinator", "point_extractor")}
    if config.reviewer and config.reviewer.enabled:
        roles["reviewer"] = config.reviewer
    elif require_reviewer:
        add("reviewer_required", "reviewer", "This entrypoint requires an enabled Reviewer.")
    if active_roles is not None:
        unknown_roles = set(active_roles) - set(roles)
        if unknown_roles:
            add("unknown_active_role", "active_roles", "Requested role is missing or disabled.")
        roles = {name: role for name, role in roles.items() if name in active_roles}
    def require_capability(alias: str, capability: str, path: str) -> None:
        declared = getattr(config.models[alias].capabilities, capability)
        if declared is not True:
            add("model_capability_unknown" if declared is None else "model_capability_unsupported",
                path, f"Deployment must explicitly declare {capability}=true with validation evidence before this operation.")

    def require_credential(alias: str) -> None:
        profile = config.models[alias]
        if not (values.get(profile.api_key_env or "") or values.get("NOVELTY_API_KEY") or values.get("LLM_API_KEY")):
            add("model_credential_missing", f"models.{alias}.api_key_env",
                "Model client requires a credential reference with a nonempty environment value.")

    for name, role in roles.items():
        profile = config.models[role.model.alias]
        if role.model.max_tokens >= profile.context_window:
            add("context_budget_conflict", f"{name}.model.max_tokens",
                "Output token limit must leave room for input within the context window.")
        require_credential(role.model.alias)
        capability = "tool_calling" if name in {"researcher", "reviewer"} else "json_object"
        require_capability(role.model.alias, capability, f"{name}.model.alias")
        choices = {"auto", "none"} if name in {"researcher", "reviewer"} else set()
        if role.model.tool_choice is not None:
            choices.add(role.model.tool_choice)
        if choices - set(profile.capabilities.tool_choices):
            add("unsupported_tool_choice", f"{name}.model.tool_choice",
                "Deployment must declare every tool_choice used by this role, including summary calls.")
        if name not in {"researcher", "reviewer"} and role.model.tool_choice not in (None, "none"):
            add("incompatible_model_option", f"{name}.model.tool_choice",
                "This role supplies no tools; a tool-selection option is incompatible.")
        for param in ("enable_thinking", "thinking_budget", "reasoning_effort"):
            value = getattr(role.model, param)
            if value not in (None, False) and param not in profile.supported_params:
                add("unsupported_model_parameter", f"{name}.model.{param}",
                    "The client would silently discard this parameter.")
    if include_processing:
        for field, alias in config.project.processing.items():
            if field not in {"llm_model", "ocr_model"} or not alias:
                continue
            if field == "ocr_model" and not config.project.processing.get("ocr_fallback_enabled", True):
                continue
            require_credential(alias)
            if field == "ocr_model":
                require_capability(alias, "vision", "project.processing.ocr_model")
    db = config.researcher.tools.database_search
    from ..tools.database_search.factory import build_source_registry
    registry = build_source_registry()
    known = set(registry._builders)
    enabled = []
    for name, provider in db.providers.items():
        if name not in known:
            add("unknown_provider", f"researcher.tools.database_search.providers.{name}", "Provider is not registered.")
        if any(type(provider.get(flag, False)) is not bool for flag in ("enabled", "testing_only")):
            add("invalid_provider_switch", f"providers.{name}",
                "enabled and testing_only must be JSON booleans, never strings.")
            continue
        if not provider.get("enabled", False) or provider.get("testing_only", False):
            continue
        enabled.append(name)
        credentials = {
            "sciencedirect": [("api_key_env", "ELSEVIER_API_KEY")],
            "ieee_xplore": [("api_key_env", "IEEE_XPLORE_API_KEY")],
            "springer": [("meta_api_key_env", "SPRINGER_NATURE_META_API_KEY")],
        }.get(name, [])
        if name == "springer":
            mode = str(provider.get("full_text_mode", "openaccess")).strip().lower()
            if mode == "openaccess":
                credentials.append(("open_access_api_key_env", "SPRINGER_NATURE_OPEN_ACCESS_API_KEY"))
            elif mode == "tdm":
                credentials.append(("tdm_api_metric_env", "SPRINGER_NATURE_TDM_API_METRIC"))
            elif mode != "disabled":
                add("invalid_provider_mode", "providers.springer.full_text_mode", "Unknown full text mode.")
        for credential, default_env in credentials:
            env_name = str(provider.get(credential, default_env) or "").strip()
            if not values.get(env_name):
                add("provider_credential_missing", f"providers.{name}.{credential}",
                    "Enabled Provider requires a credential; availability remains unverified.")
    for name in config.project.workflow.recovery_provider_order:
        if name not in known or name not in enabled:
            add("recovery_provider_unavailable", "project.workflow.recovery_provider_order",
                "Recovery Providers must be registered, enabled and outside testing-only mode.")
    if len(set(config.project.workflow.recovery_provider_order)) != len(config.project.workflow.recovery_provider_order):
        add("duplicate_recovery_provider", "project.workflow.recovery_provider_order", "Recovery order must not repeat Providers.")
    if db.active_source not in enabled:
        add("active_provider_unavailable", "researcher.tools.database_search.active_source",
            "Active source must be enabled and available outside testing-only mode.")
    if config.researcher.tools.web_search.enabled and not values.get("BAIDU_QIANFAN_API_KEY"):
        add("provider_credential_missing", "researcher.tools.web_search.baidu",
            "Enabled Baidu search requires BAIDU_QIANFAN_API_KEY.")
    for role, operations in configured_prompt_names(config).items():
        if role not in roles:
            continue
        for operation, name in operations.items():
            if _prompt_file(name) is None:
                add("prompt_missing", f"{role}.{operation}",
                    "Selected prompt must exist inside the configured prompt library.")
    allowed_legacy = {
        "coordinator": {"coordinator/plan", "coordinator/plan_supplement", "coordinator/supplement", "coordinator/synthesize"},
        "point_extractor": {"extractor/read_paper", "extractor/extract_points", "reviewer/review_points"},
    }
    for role, allowed in allowed_legacy.items():
        if role not in roles:
            continue
        inventory = getattr(config, role).prompts
        if inventory:
            custom = set(inventory) - allowed
            add("legacy_prompt_selection_ignored" if custom else "legacy_prompt_inventory",
                f"{role}.prompts", "Use named prompt_names operations; legacy lists are inventory only.",
                "error" if custom else "warning")
    for role, settings in roles.items():
        admission = config.models[settings.model.alias].context_admission
        if admission.mode == "enforce" and admission.counter == "none":
            add("context_counter_required", f"models.{settings.model.alias}.context_admission",
                "Enforcement needs an exact counter; character estimates are not supported.")
    if any(config.models[role.model.alias].context_admission.mode != "enforce" or
           config.models[role.model.alias].context_admission.on_unavailable != "reject"
           for role in roles.values()):
        add("context_input_not_preflighted", "models.context_window",
            "Output limits are checked; some active models do not enforce exact input admission.", "warning")
    return issues


class ConfigurationPreflightError(ValueError):
    """Offline configuration rejection; messages contain references, never secrets."""

    def __init__(self, issues: list[dict[str, str]]) -> None:
        self.issues = issues
        details = "; ".join(f"{item['code']} at {item['path']}: {item['message']}"
                            for item in issues if item["severity"] == "error")
        super().__init__("configuration preflight failed: " + details)


def require_preflight(config: ApplicationConfig, **kwargs: Any) -> list[dict[str, str]]:
    issues = preflight_config(config, **kwargs)
    if any(item["severity"] == "error" for item in issues):
        raise ConfigurationPreflightError(issues)
    return issues


def _startup_source_files() -> list[Path]:
    """Execution source only: never environment files, run artifacts or credentials."""
    paths = {p for folder in (PROJECT_ROOT / "backend", PROJECT_ROOT / "scripts")
             for p in folder.rglob("*.py") if "__pycache__" not in p.parts}
    paths.update(PROMPTS_ROOT.rglob("*.md"))
    for folder in ("templates", "skills"):
        paths.update((PROMPTS_ROOT.parent / folder).rglob("*.md"))
    pricing = PROMPTS_ROOT.parent / "config" / "llm_pricing.json"
    if pricing.is_file():
        paths.add(pricing)
    # Config values are saved as the redacted effective configuration, not raw files.
    for name in ("pyproject.toml", "requirements.txt", "requirements-dev.txt", "uv.lock", "poetry.lock", "Pipfile.lock"):
        if (PROJECT_ROOT / name).is_file():
            paths.add(PROJECT_ROOT / name)
    return sorted(paths)


def verify_startup_snapshot(directory: Path) -> bool:
    """Verify immutable saved bytes independently of the current working tree."""
    directory = Path(directory)
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    for relative, expected in manifest["snapshot_files"].items():
        path = (directory / relative).resolve()
        if not path.is_relative_to(directory.resolve()) or not path.is_file():
            raise ValueError("startup snapshot file missing or outside snapshot")
        if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise ValueError(f"startup snapshot checksum mismatch: {relative}")
    actual_files = {p.relative_to(directory).as_posix() for p in directory.rglob("*") if p.is_file() and p.name != "manifest.json"}
    if actual_files != set(manifest["snapshot_files"]):
        raise ValueError("startup snapshot contains unlisted files")
    if _sha(manifest["snapshot_files"]) != manifest["snapshot_sha256"]:
        raise ValueError("startup snapshot manifest checksum mismatch")
    return True


def prepare_startup(config: ApplicationConfig, *, output_root: Path,
                    entrypoint: str, snapshot_dir: Path | None = None,
                    input_path: Path | None = None,
                    input_contents: Mapping[str, bytes] | None = None,
                    include_processing: bool = False,
                    active_roles: tuple[str, ...] | None = None,
                    require_reviewer: bool = True,
                    runtime_controls: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Validate and save a run-scoped, reconstructable startup tree before any I/O calls.

    The archive captures dirty files as bytes, plus selected configuration and inputs.
    It does not claim to freeze external services, tokenizer servers or secret values.
    """
    issues = require_preflight(config, include_processing=include_processing,
                               active_roles=active_roles, require_reviewer=require_reviewer)
    if config._startup_manifest:
        previous = config._startup_manifest
        current = freeze_config(config, entrypoint=entrypoint, output_root=output_root)
        if current["effective_config_sha256"] != previous["effective_config_sha256"]:
            raise ValueError("configuration changed after startup snapshot")
        if input_path is not None and (previous.get("input") or {}).get("sha256") != hashlib.sha256(input_path.read_bytes()).hexdigest():
            raise ValueError("input changed after startup snapshot")
        for name, data in (input_contents or {}).items():
            if previous["code"]["source_snapshot"]["snapshot_files"].get("inputs/" + name) != hashlib.sha256(data).hexdigest():
                raise ValueError("input contents changed after startup snapshot")
        verify_startup_snapshot(Path(previous["code"]["source_snapshot"]["path"]))
        expected_sources = previous["code"]["source_snapshot"]["source_files"]
        current_sources = {p.relative_to(PROJECT_ROOT).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                           for p in _startup_source_files()}
        if current_sources != expected_sources:
            raise ValueError("source changed after startup snapshot; restart with a new snapshot")
        return copy.deepcopy(previous)
    frozen = freeze_config(config, entrypoint=entrypoint, input_path=input_path, output_root=output_root)
    source_bytes = {p.relative_to(PROJECT_ROOT).as_posix(): p.read_bytes() for p in _startup_source_files()}
    source_hashes = {name: hashlib.sha256(data).hexdigest() for name, data in source_bytes.items()}
    # Reject a torn capture when a development edit races this startup.
    current_hashes = {p.relative_to(PROJECT_ROOT).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                      for p in _startup_source_files()}
    if current_hashes != source_hashes:
        raise ValueError("source tree changed during startup snapshot; retry after edits finish")
    frozen["code"]["python_sources_sha256"] = _sha({name: digest for name, digest in source_hashes.items() if name.endswith(".py")})
    frozen["prompt_sha256"] = {str(Path(name).relative_to(PROMPTS_ROOT.relative_to(PROJECT_ROOT))): digest
                              for name, digest in source_hashes.items()
                              if Path(name).is_relative_to(PROMPTS_ROOT.relative_to(PROJECT_ROOT))}
    from backend.env.prompt_library import parse_front_matter
    for role, operations in configured_prompt_names(config).items():
        for operation, name in operations.items():
            relative = (PROMPTS_ROOT.relative_to(PROJECT_ROOT) / f"{name}.md").as_posix()
            content = source_bytes.get(relative)
            metadata = parse_front_matter(content.decode("utf-8"))[0] if content is not None else {}
            frozen["selected_prompts"][role][operation] = {
                "name": name, "version": metadata.get("version"), "exists": content is not None,
                "sha256": hashlib.sha256(content).hexdigest() if content is not None else None,
            }
    directory = Path(snapshot_dir or (Path(output_root) / ".startup" / uuid.uuid4().hex)).resolve()
    directory.parent.mkdir(parents=True, exist_ok=True)
    directory.mkdir(exist_ok=False)
    hashes: dict[str, str] = {}

    def save(relative: str, data: bytes) -> None:
        target = directory / relative
        if not target.resolve().is_relative_to(directory):
            raise ValueError("startup input name must stay within snapshot")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        hashes[relative] = hashlib.sha256(data).hexdigest()

    for name, data in source_bytes.items():
        save("source/" + name, data)
    save("effective-config.json", (json.dumps(frozen["effective_config"], ensure_ascii=False, indent=2) + "\n").encode())
    # Reconstruct the default split-file loader too, without copying raw profiles
    # that might contain embedded credentials. These are generated effective values.
    config_root = Path(__file__).resolve().parent.relative_to(PROJECT_ROOT)
    split_names = {"project": "settings.example.json", "models": "models.example.json",
                   **{role: f"agents/{role}.example.json" for role in
                      ("researcher", "search_planner", "reviewer", "coordinator", "point_extractor")}}
    generated_config_files = []
    for key, name in split_names.items():
        relative = (Path("source") / config_root / name).as_posix()
        save(relative, (json.dumps(frozen["effective_config"][key], ensure_ascii=False, indent=2) + "\n").encode())
        generated_config_files.append(relative)
    inputs = dict(input_contents or {})
    if input_path is not None:
        input_bytes = input_path.read_bytes()
        if hashlib.sha256(input_bytes).hexdigest() != frozen["input"]["sha256"]:
            raise ValueError("input changed during startup snapshot")
        inputs.setdefault("primary-input" + input_path.suffix, input_bytes)
    for name, data in inputs.items():
        save("inputs/" + name, data)
    frozen["preflight"] = issues
    frozen["captured_at"] = datetime.now(UTC).isoformat()
    frozen["execution_scope"] = {"active_roles": list(active_roles) if active_roles is not None else "all_enabled_roles",
                                 "include_processing": include_processing, "require_reviewer": require_reviewer}
    frozen["environment"]["model_server_tokenizer"] = {"version": None, "status": "not_probed",
        "note": "Installed client tokenizer packages are recorded separately; remote server/tokenizer versions require deployment evidence."}
    frozen["runtime_controls"] = redact_config(runtime_controls or {})
    frozen["code"]["source_snapshot"] = {
        "path": str(directory), "source_file_count": len(source_bytes),
        "source_files": source_hashes, "source_tree_sha256": _sha(source_hashes),
        "snapshot_files": hashes, "snapshot_sha256": _sha(hashes),
        "generated_effective_config_files": generated_config_files,
        "config_reconstruction": "ApplicationConfig.model_validate(effective-config.json); supply credential environment separately",
        "scope": "startup source/config/input bytes; external services and later source edits are not frozen",
    }
    manifest = {**frozen, "snapshot_files": hashes, "snapshot_sha256": _sha(hashes)}
    (directory / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    verify_startup_snapshot(directory)
    config._startup_manifest = copy.deepcopy(frozen)
    return frozen
