"""Prepare a run-scoped subject-reference snapshot for PaperInput workflows."""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path
from collections.abc import Mapping
from typing import Any

from pydantic import ValidationError

from ..persistence import (
    SubjectReferenceStore,
    persist_workflow_input,
    subject_reference_workspace,
)
from ..schemas import PaperInput, ReferenceBootstrapManifest
from ..tools.database_search.providers.arxiv import build_arxiv_search_tool
from .reference_bootstrap import (
    ReferenceBootstrapService,
    ReferenceProviderRegistry,
    references_digest,
)


class PaperInputReferenceBootstrapError(RuntimeError):
    """The PaperInput pipeline cannot safely use its subject-reference pool."""


def prepare_paper_input_references(
    paper: PaperInput,
    *,
    stable_output_root: str | Path,
    run_output_root: str | Path,
    force: bool = False,
    max_concurrency: int = 4,
    service: ReferenceBootstrapService | None = None,
    arxiv_options: Mapping[str, Any] | None = None,
) -> ReferenceBootstrapManifest:
    """Validate/refresh the stable cache and snapshot it into one isolated run."""

    stable_root = Path(stable_output_root)
    run_root = Path(run_output_root)
    persist_workflow_input(paper, output_root=run_root)
    stable_store = SubjectReferenceStore(stable_root)
    cached = _load_valid_cache(stable_store, paper)

    if force or cached is None:
        bootstrap = service or ReferenceBootstrapService(
            ReferenceProviderRegistry([build_arxiv_search_tool(arxiv_options)]),
            stable_store,
            max_concurrency=max_concurrency,
        )
        try:
            cached = asyncio.run(
                bootstrap.bootstrap(
                    paper.paper_id,
                    paper.references,
                    force=True,
                )
            )
        except Exception as exc:
            raise PaperInputReferenceBootstrapError(
                f"subject-reference bootstrap failed: {type(exc).__name__}: {exc}"
            ) from exc

    if not _manifest_matches(cached, paper):
        raise PaperInputReferenceBootstrapError(
            "subject-reference bootstrap is not ready for current PaperInput"
        )

    source = subject_reference_workspace(paper, output_root=stable_root)
    destination = subject_reference_workspace(paper, output_root=run_root)
    shutil.copytree(source, destination, dirs_exist_ok=True)
    snapshot = _load_valid_cache(SubjectReferenceStore(run_root), paper)
    if snapshot is None:
        raise PaperInputReferenceBootstrapError(
            "run-scoped subject-reference snapshot failed validation"
        )
    return snapshot


def _load_valid_cache(
    store: SubjectReferenceStore, paper: PaperInput
) -> ReferenceBootstrapManifest | None:
    try:
        manifest = store.load_bootstrap(paper.paper_id)
    except (OSError, ValueError, ValidationError):
        return None
    return manifest if _manifest_matches(manifest, paper) else None


def _manifest_matches(
    manifest: ReferenceBootstrapManifest, paper: PaperInput
) -> bool:
    return (
        manifest.subject_paper_id == paper.paper_id
        and manifest.references_digest == references_digest(paper.references)
        and len(manifest.entries) == len(paper.references)
        and manifest.bootstrap_ready
    )
