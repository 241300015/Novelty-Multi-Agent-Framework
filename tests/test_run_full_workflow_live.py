from __future__ import annotations

import json
import sys
from concurrent.futures import ThreadPoolExecutor

import pytest

from scripts import run_full_workflow_live as live


def test_run_directory_allocation_starts_at_0001_and_increments(tmp_path):
    root = tmp_path / "runs"

    first_number, first = live.allocate_run_directory(root)
    second_number, second = live.allocate_run_directory(root)

    assert (first_number, first.name) == (1, "0001")
    assert (second_number, second.name) == (2, "0002")


def test_run_directory_allocation_ignores_non_numeric_directories(tmp_path):
    root = tmp_path / "runs"
    (root / "0001").mkdir(parents=True)
    (root / "0002").mkdir()
    (root / "notes").mkdir()

    number, path = live.allocate_run_directory(root)

    assert (number, path.name) == (3, "0003")


def test_explicit_run_number_never_overwrites(tmp_path):
    root = tmp_path / "runs"
    number, path = live.allocate_run_directory(root, 4)

    assert (number, path.name) == (4, "0004")
    with pytest.raises(FileExistsError):
        live.allocate_run_directory(root, 4)


def test_concurrent_run_directory_allocation_is_unique(tmp_path):
    root = tmp_path / "runs"

    with ThreadPoolExecutor(max_workers=8) as executor:
        allocated = list(executor.map(lambda _: live.allocate_run_directory(root), range(8)))

    assert sorted(number for number, _ in allocated) == list(range(1, 9))
    assert len({path for _, path in allocated}) == 8


def test_paper_input_entrypoint_uses_numbered_workspace(tmp_path, monkeypatch) -> None:
    paper_json = tmp_path / "paper.json"
    paper_json.write_text(
        json.dumps({"paper_id": "paper-1", "title": "Paper", "full_text": "body"}),
        encoding="utf-8",
    )
    runs_root = tmp_path / "runs"
    captured = {}

    class Result:
        def model_dump_json(self):
            return json.dumps({"ok": True})

    class Workflow:
        last_runtime_run_id = "run-runtime-id"
        last_rendered_report_path = "/run/0001/paper-1/report/report.md"
        last_rendered_report_paths = {
            "default": last_rendered_report_path,
            "concise": "/run/0001/paper-1/report/paper-1-concise.md",
        }

        def run(self, paper, *, run_identity=None):
            captured["paper"] = paper
            captured["run_identity"] = run_identity
            return Result()

    from novelty_agent_framework.config import load_application_config
    config = load_application_config(environ={})
    monkeypatch.setattr(live, "_load_dev_env", lambda: None)
    monkeypatch.setenv("LOCAL_VLLM_API_KEY", "offline-test")
    monkeypatch.setattr(live, "load_application_config", lambda **kwargs: config)
    monkeypatch.setattr(live, "preflight_config", lambda config: [])

    def build(_config, *, output_root):
        captured["output_root"] = output_root
        return Workflow()

    monkeypatch.setattr(live, "build_standard_full_workflow", build)
    monkeypatch.setattr(
        live,
        "prepare_paper_input_references",
        lambda paper, **kwargs: captured.update(bootstrap=(paper, kwargs)),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_full_workflow_live.py",
            "--paper-json",
            str(paper_json),
            "--runs-root",
            str(runs_root),
        ],
    )

    live.main()

    run_dir = runs_root / "0001"
    identity = captured["run_identity"]
    manifest = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    assert captured["paper"].paper_id == "paper-1"
    assert captured["output_root"] == run_dir.resolve()
    assert captured["bootstrap"][1]["run_output_root"] == run_dir.resolve()
    assert captured["bootstrap"][1]["arxiv_options"]["search_transport"] == "web"
    assert identity["entrypoint"] == "paper_input"
    assert identity["run_number"] == 1
    assert len(identity["input_identity"]["paper_sha256"]) == 64
    assert json.loads((run_dir / "result.json").read_text(encoding="utf-8")) == {
        "ok": True
    }
    assert manifest["status"] == "SUCCESS"
    assert manifest["runtime_run_id"] == "run-runtime-id"
    assert manifest["rendered_report_paths"] == Workflow.last_rendered_report_paths
    assert manifest["result_path"] == str((run_dir / "result.json").resolve())


def test_invalid_secret_in_config_never_enters_failed_run_manifest(tmp_path):
    from novelty_agent_framework.config import load_application_config
    from pydantic import ValidationError
    manifest = {}
    try:
        load_application_config(environ={}, overrides={
            'models': {'local-qwen2.5-7b': {'api_key': 'SENTINEL_CONFIG_SECRET'}}})
    except ValidationError as error:
        assert 'SENTINEL_CONFIG_SECRET' not in str(error)
        live._finish_manifest(manifest, tmp_path / 'run.json', status='FAILED',
                              started=live.time.monotonic(), workflow=None, error=error)
    serialized = (tmp_path / 'run.json').read_text()
    assert 'SENTINEL_CONFIG_SECRET' not in serialized
    assert 'extra_forbidden' in serialized
    assert manifest['status'] == 'FAILED'


def test_direct_entrypoint_uses_this_checkout_before_installed_framework(tmp_path):
    """A fresh process must not resolve an editable install from another worktree."""
    import os
    from pathlib import Path
    import subprocess

    foreign = tmp_path / 'foreign_install' / 'novelty_agent_framework'
    foreign.mkdir(parents=True)
    (foreign / '__init__.py').write_text("raise AssertionError('foreign framework imported')\n")
    script = Path(live.__file__).resolve()
    expected = script.parents[1] / 'backend/src/novelty_agent_framework'
    code = (
        "import runpy,sys; from pathlib import Path; "
        "runpy.run_path(sys.argv[1],run_name='entrypoint_origin_probe'); "
        "import novelty_agent_framework; "
        "assert Path(novelty_agent_framework.__file__).resolve().parent == Path(sys.argv[2])"
    )
    completed = subprocess.run([sys.executable, '-c', code, str(script), str(expected)],
        cwd=tmp_path, env={**os.environ, 'PYTHONPATH': str(foreign.parent),
                          'PYTHONDONTWRITEBYTECODE': '1'}, capture_output=True, text=True, timeout=20)
    assert completed.returncode == 0, completed.stderr


def test_entrypoint_rejects_framework_already_imported_from_other_checkout(tmp_path):
    import os
    from pathlib import Path
    import subprocess

    foreign = tmp_path / 'foreign_install' / 'novelty_agent_framework'
    foreign.mkdir(parents=True)
    (foreign / '__init__.py').write_text('# deliberately preloaded foreign package\n')
    script = Path(live.__file__).resolve()
    code = """import runpy, sys
import novelty_agent_framework
try:
    runpy.run_path(sys.argv[1], run_name='entrypoint_origin_probe')
except RuntimeError as exc:
    assert 'framework import belongs to another checkout' in str(exc)
else:
    raise AssertionError('foreign cached framework was accepted')
"""
    completed = subprocess.run([sys.executable, '-c', code, str(script)], cwd=tmp_path,
        env={**os.environ, 'PYTHONPATH': str(foreign.parent), 'PYTHONDONTWRITEBYTECODE':'1'},
        capture_output=True, text=True, timeout=20)
    assert completed.returncode == 0, completed.stderr
