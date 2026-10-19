"""Stable Runtime Debug identities for file-backed experiment inputs."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def display_path(path: Path, *, project_root: Path) -> str:
    resolved = Path(path).resolve(strict=True)
    root = Path(project_root).resolve(strict=True)
    try:
        return resolved.relative_to(root).as_posix()
    except ValueError:
        return str(resolved)


def file_run_identity(
    entrypoint: str,
    paper_json: Path,
    *,
    project_root: Path,
    novelty_point_id: str | None = None,
    task_id: str | None = None,
    search_plan_id: str | None = None,
) -> dict[str, Any]:
    path = Path(paper_json).resolve(strict=True)
    return {
        "entrypoint": entrypoint,
        "input_identity": {
            "paper_json": display_path(path, project_root=project_root),
            "paper_sha256": sha256_file(path),
            "novelty_point_id": novelty_point_id,
            "task_id": task_id,
            "search_plan_id": search_plan_id,
        },
    }


__all__ = ["display_path", "file_run_identity", "sha256_file"]
