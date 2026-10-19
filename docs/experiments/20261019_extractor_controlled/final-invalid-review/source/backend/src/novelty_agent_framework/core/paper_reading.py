"""Versioned target-paper reading through the existing input content-list contract."""
from __future__ import annotations

import hashlib
import json
import uuid
from pathlib import Path

from ..schemas.domain import PaperInput, PaperReadingReference
from ..schemas.failures import FailureCode

CHUNK_CHARS = 4000
INDEX_VERSION = 1


class PaperReadingError(ValueError):
    def __init__(self, code: FailureCode, message: str):
        self.code = code
        super().__init__(f"{code.value}: {message}")


def _segments(text: str) -> list[dict]:
    # Exact Python Unicode character offsets, including line breaks; no normalization.
    return [{"segment_id": f"body-{i // CHUNK_CHARS + 1}", "start": i,
             "end": min(i + CHUNK_CHARS, len(text)),
             "preview": text[i:i + 100]}
            for i in range(0, len(text), CHUNK_CHARS)]


def persist_reading_snapshot(paper: PaperInput, workspace: Path, *, stage: str) -> PaperReadingReference:
    from ..persistence import _atomic_write_json
    from .runtime_artifacts import current_runtime_artifacts

    runtime = current_runtime_artifacts()
    snapshot_id = uuid.uuid4().hex
    run_id = runtime.run_id if runtime else f"input-{snapshot_id}"
    directory = workspace / "paper-input" / "snapshots" / snapshot_id
    directory.mkdir(parents=True, exist_ok=False)
    paper_path = directory / "paper.json"
    _atomic_write_json(paper_path, paper.model_dump(mode="json"))
    index = {
        "schema_version": INDEX_VERSION, "run_id": run_id, "paper_id": paper.paper_id,
        "artifacts": [{"artifact_type": "paper_input", "relative_path": "paper.json",
                       "version": 1, "sha256": hashlib.sha256(paper_path.read_bytes()).hexdigest(),
                       "generated_stage": stage, "status": "ready"}],
        "segments": _segments(paper.full_text),
    }
    index_path = directory / "content-list.json"
    _atomic_write_json(index_path, index)
    reference = PaperReadingReference(workspace=str(workspace.resolve()),
        index_path=str(index_path.relative_to(workspace)), run_id=run_id,
        paper_id=paper.paper_id, sha256=hashlib.sha256(index_path.read_bytes()).hexdigest())
    # The existing content-list remains the discoverable entry point. Historical
    # results carry their frozen reference and never follow this mutable pointer.
    current_path = workspace / "paper-input" / "content-list.json"
    current = json.loads(current_path.read_text(encoding="utf-8"))
    current["reading_index"] = reference.model_dump(mode="json", exclude={"workspace"})
    _atomic_write_json(current_path, current)
    if runtime is not None:
        runtime.record_paper_reading_index(reference.model_dump(mode="json"))
    return reference


def latest_reading_reference(workspace: Path) -> PaperReadingReference:
    data = _load_json(workspace / "paper-input" / "content-list.json")
    try:
        return PaperReadingReference(workspace=str(workspace.resolve()), **data["reading_index"])
    except (KeyError, TypeError, ValueError) as exc:
        raise PaperReadingError(FailureCode.READER_MANIFEST_INVALID, "missing reading index") from exc


def _read(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except FileNotFoundError as exc:
        raise PaperReadingError(FailureCode.READER_FILE_MISSING, str(path)) from exc
    except PermissionError as exc:
        raise PaperReadingError(FailureCode.READER_ACCESS_DENIED, str(path)) from exc
    except OSError as exc:
        raise PaperReadingError(FailureCode.READER_VERIFICATION_FAILED, str(path)) from exc


def _load_json(path: Path, expected_hash: str | None = None):
    raw = _read(path)
    if expected_hash and hashlib.sha256(raw).hexdigest() != expected_hash:
        raise PaperReadingError(FailureCode.READER_HASH_MISMATCH, str(path))
    try:
        return json.loads(raw.decode("utf-8"))
    except UnicodeDecodeError as exc:
        raise PaperReadingError(FailureCode.READER_DECODE_FAILED, str(path)) from exc
    except ValueError as exc:
        raise PaperReadingError(FailureCode.READER_MANIFEST_INVALID, str(path)) from exc


def _contained(root: Path, relative: str) -> Path:
    path = (root / relative).resolve()
    if Path(relative).is_absolute() or not path.is_relative_to(root.resolve()):
        raise PaperReadingError(FailureCode.READER_PATH_ESCAPE, relative)
    return path


class PaperReader:
    """No cached body: every read reopens and verifies persisted index and input."""
    def __init__(self, reference: PaperReadingReference):
        self.reference = reference

    def load(self) -> tuple[PaperInput, dict]:
        ref = self.reference
        path = _contained(Path(ref.workspace), ref.index_path)
        index = _load_json(path, ref.sha256)
        try:
            if (index["schema_version"] != INDEX_VERSION or index["run_id"] != ref.run_id
                    or index["paper_id"] != ref.paper_id):
                raise ValueError("index version or identity mismatch")
            artifact, = index["artifacts"]
            if (artifact["artifact_type"] != "paper_input" or artifact["version"] != 1
                    or artifact["status"] != "ready" or not artifact["sha256"]):
                raise ValueError("unreadable input artifact")
            paper_path = _contained(path.parent, artifact["relative_path"])
            paper = PaperInput.model_validate(_load_json(paper_path, artifact["sha256"]))
            if paper.paper_id != ref.paper_id or index["segments"] != _segments(paper.full_text):
                raise ValueError("paper identity or segment ranges mismatch")
        except PaperReadingError:
            raise
        except (KeyError, TypeError, ValueError) as exc:
            raise PaperReadingError(FailureCode.READER_MANIFEST_INVALID, str(exc)) from exc
        return paper, index

    def read(self, segment_id: str) -> dict:
        paper, index = self.load()
        for item in index["segments"]:
            if item["segment_id"] == segment_id:
                ref = self.reference
                return {**item, "text": paper.full_text[item["start"]:item["end"]],
                        "source_location": f"paper:{ref.run_id}:{ref.sha256}:full_text[{item['start']}:{item['end']}]"}
        raise PaperReadingError(FailureCode.TOOL_ARGUMENT_SCHEMA, f"unknown segment: {segment_id}")
