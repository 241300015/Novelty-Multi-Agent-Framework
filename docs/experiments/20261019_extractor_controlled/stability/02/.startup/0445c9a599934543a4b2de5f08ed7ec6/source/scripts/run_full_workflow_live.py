"""Run the standard PaperInput workflow in a numbered isolated workspace.

Example:
    python scripts/run_full_workflow_live.py \
        --paper-json outputs/MG19333vrw/paper-input/others/paper.json \
        --runs-root outputs/runs --run-number 4 \
        --max-rounds 1 --max-concurrency 4
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.env.model_client import _load_dev_env
from novelty_agent_framework.config import (
    build_standard_full_workflow,
    load_application_config,
    ReviewerRequiredError,
)
from novelty_agent_framework.config.experiment import freeze_config, preflight_config, redact_config, prepare_startup
from pydantic import ValidationError
from novelty_agent_framework.core.run_identity import file_run_identity
from novelty_agent_framework.processing import prepare_paper_input_references
from novelty_agent_framework.schemas import PaperInput

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--paper-json", required=True, type=Path)
    result.add_argument("--runs-root", type=Path, default=Path("outputs/runs"))
    result.add_argument("--run-number", type=int)
    result.add_argument("--profile", type=Path)
    result.add_argument("--isolated-config", action="store_true",
                        help="Ignore configuration environment overrides; credentials still resolve normally.")
    result.add_argument("--check-config", action="store_true")
    result.add_argument("--max-rounds", type=int)
    result.add_argument("--max-concurrency", type=int)
    result.add_argument("--max-model-calls", type=int)
    result.add_argument("--max-physical-provider-requests", type=int)
    result.add_argument("--force-reference-bootstrap", action="store_true")
    return result


def allocate_run_directory(
    runs_root: str | Path, run_number: int | None = None
) -> tuple[int, Path]:
    """Atomically reserve a pure-numeric run directory without overwriting."""

    root = Path(runs_root)
    root.mkdir(parents=True, exist_ok=True)
    if run_number is not None:
        if run_number < 1:
            raise ValueError("run-number must be positive")
        path = root / f"{run_number:04d}"
        path.mkdir(exist_ok=False)
        return run_number, path

    existing = [
        int(item.name)
        for item in root.iterdir()
        if item.is_dir() and item.name.isdigit()
    ]
    candidate = max(existing, default=0) + 1
    while True:
        path = root / f"{candidate:04d}"
        try:
            path.mkdir(exist_ok=False)
        except FileExistsError:
            candidate += 1
            continue
        return candidate, path


def main() -> None:
    _load_dev_env()
    args = parser().parse_args()
    paper_json = args.paper_json.resolve(strict=True)
    paper = PaperInput.model_validate_json(paper_json.read_text(encoding="utf-8"))
    run_number, run_dir = allocate_run_directory(args.runs_root, args.run_number)
    run_dir = run_dir.resolve()
    identity = file_run_identity("paper_input", paper_json, project_root=PROJECT_ROOT)
    identity["run_number"] = run_number
    started_at = datetime.now(UTC)
    monotonic_started = time.monotonic()
    run_manifest: dict[str, Any] = {
        "run_number": run_number,
        "status": "RUNNING",
        "entrypoint": "paper_input",
        "paper_id": paper.paper_id,
        "paper_json_source": identity["input_identity"]["paper_json"],
        "paper_sha256": identity["input_identity"]["paper_sha256"],
        "git_commit": _git_commit(),
        "max_rounds": args.max_rounds,
        "max_concurrency": args.max_concurrency,
        "max_model_calls": args.max_model_calls,
        "max_physical_provider_requests": args.max_physical_provider_requests,
        "started_at": started_at.isoformat(),
    }
    manifest_path = run_dir / "run.json"
    _write_json(manifest_path, run_manifest)

    workflow = None
    try:
        overrides = {"project": {"workflow": {}, "runtime_debug": {}}}
        for field in ("max_rounds", "max_concurrency"):
            if getattr(args, field) is not None:
                overrides["project"]["workflow"][field] = getattr(args, field)
        for field in ("max_model_calls", "max_physical_provider_requests"):
            if getattr(args, field) is not None:
                overrides["project"]["runtime_debug"][field] = getattr(args, field)
        config = load_application_config(profile_path=args.profile, overrides=overrides,
                                         environ={} if args.isolated_config else None)
        frozen = freeze_config(config, entrypoint="paper_input", input_path=paper_json, output_root=run_dir)
        issues = preflight_config(config)
        _write_json(run_dir / "effective-config.json", frozen["effective_config"])
        _write_json(run_dir / "experiment-manifest.json", frozen)
        _write_json(run_dir / "preflight.json", issues)
        run_manifest.update(max_rounds=config.project.workflow.max_rounds,
                            max_concurrency=config.project.workflow.max_concurrency,
                            max_model_calls=config.project.runtime_debug.max_model_calls,
                            max_physical_provider_requests=config.project.runtime_debug.max_physical_provider_requests,
                            effective_config_sha256=frozen["effective_config_sha256"])
        if any(item["code"] == "reviewer_required" for item in issues):
            raise ReviewerRequiredError("reviewer_required: reviewer.enabled=false or reviewer config missing")
        if any(item["severity"] == "error" for item in issues):
            raise ValueError("configuration preflight failed; see preflight.json")
        if args.check_config:
            _finish_manifest(run_manifest, manifest_path, status="PREFLIGHT_PASSED",
                             started=monotonic_started, workflow=None)
            print(json.dumps(issues, ensure_ascii=False, indent=2))
            return
        frozen = prepare_startup(config, entrypoint="paper_input", input_path=paper_json,
                                 output_root=run_dir, snapshot_dir=run_dir / "startup",
                                 input_contents={"paper-input.json": paper.model_dump_json().encode()})
        _write_json(run_dir / "experiment-manifest.json", frozen)
        _write_json(run_dir / "paper-input.json", paper.model_dump(mode="json"))
        workflow = build_standard_full_workflow(config, output_root=run_dir)
        stable_root = _stable_output_root(paper_json, paper.paper_id)
        prepare_paper_input_references(
            paper,
            stable_output_root=stable_root,
            run_output_root=run_dir,
            force=args.force_reference_bootstrap,
            arxiv_options=config.researcher.tools.database_search.providers["arxiv"],
            max_concurrency=config.project.workflow.max_concurrency,
        )
        result = workflow.run(paper, run_identity=identity)
        payload = json.loads(result.model_dump_json())
        result_path = run_dir / "result.json"
        _write_json(result_path, payload)
    except KeyboardInterrupt as exc:
        _finish_manifest(
            run_manifest,
            manifest_path,
            status="INTERRUPTED",
            started=monotonic_started,
            workflow=workflow,
            error=exc,
        )
        raise
    except BaseException as exc:
        _finish_manifest(
            run_manifest,
            manifest_path,
            status="FAILED",
            started=monotonic_started,
            workflow=workflow,
            error=exc,
        )
        raise
    else:
        _finish_manifest(
            run_manifest,
            manifest_path,
            status="SUCCESS",
            started=monotonic_started,
            workflow=workflow,
            result_path=result_path,
        )
        print(json.dumps(payload, ensure_ascii=False, indent=2))


def _stable_output_root(paper_json: Path, paper_id: str) -> Path:
    """Recognize the canonical outputs/<paper_id>/paper-input/others path."""

    if (
        paper_json.parent.name == "others"
        and paper_json.parent.parent.name == "paper-input"
        and paper_json.parent.parent.parent.name == paper_id
    ):
        return paper_json.parent.parent.parent.parent
    return Path("outputs").resolve()


def _finish_manifest(
    manifest: dict[str, Any],
    path: Path,
    *,
    status: str,
    started: float,
    workflow: Any,
    result_path: Path | None = None,
    error: BaseException | None = None,
) -> None:
    manifest.update(
        {
            "status": status,
            "finished_at": datetime.now(UTC).isoformat(),
            "duration": round(time.monotonic() - started, 6),
            "runtime_run_id": getattr(workflow, "last_runtime_run_id", None),
            "result_path": str(result_path) if result_path is not None else None,
            "rendered_report_path": getattr(
                workflow, "last_rendered_report_path", None
            ),
        }
    )
    if error is not None:
        if isinstance(error, ValidationError):
            # Never serialize Pydantic's input_value: a malformed config may
            # itself contain a misplaced credential before snapshot redaction.
            details = [{"type": item["type"], "loc": item["loc"]}
                       for item in error.errors(include_input=False, include_context=False, include_url=False)]
            manifest["error"] = "ValidationError: invalid configuration or input"
            manifest["validation_errors"] = details
        else:
            manifest["error"] = redact_config(f"{type(error).__name__}: {error}"[:1000])
    _write_json(path, manifest)


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _git_commit() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=PROJECT_ROOT,
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except (OSError, subprocess.SubprocessError):
        return "unknown"


if __name__ == "__main__":
    main()
