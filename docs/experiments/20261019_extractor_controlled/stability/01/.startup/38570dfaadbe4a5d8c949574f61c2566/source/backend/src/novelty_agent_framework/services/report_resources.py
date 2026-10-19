"""Local, read-only published report resources, separate from business runs."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path

from novelty_agent_framework.core.integrity_gates import validate_report_integrity
from novelty_agent_framework.core.report_binding import assemble_report_from_draft
from novelty_agent_framework.schemas import EvidenceCard, NoveltyPoint, NoveltyPointReview, NoveltyReport, ReportNarrativeDraft

logger = logging.getLogger("uvicorn.error.report_resources")
_ID = re.compile(r"^rr-[0-9a-f]{32}$")
_FILES = {"report.md": "text/markdown", "report.json": "application/json", "provenance.json": "application/json"}


class ReportResourceError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _json(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise ReportResourceError("invalid_manifest", f"Invalid JSON: {path.name}") from exc


def verify_bundle(bundle: Path) -> dict:
    """Check identities, original bytes, and the production report integrity gate."""
    manifest = _json(bundle / "manifest.json")
    if manifest.get("report_kind") != "captured_response_reassembly":
        raise ReportResourceError("wrong_report_kind", "Only captured response reassembly is publishable")
    if manifest.get("source_run_status") != "FAILED" or manifest.get("upstream_recomputed") is not False:
        raise ReportResourceError("invalid_provenance", "Source status or recovery scope is invalid")
    files = manifest.get("files")
    if not isinstance(files, dict) or set(files) != set(_FILES):
        raise ReportResourceError("invalid_manifest", "Unexpected publication files")
    for name in _FILES:
        path = bundle / name
        if path.is_symlink() or not path.is_file() or path.parent != bundle:
            raise ReportResourceError("missing_artifact", f"Missing or linked {name}")
        data = path.read_bytes()
        if files[name] != {"sha256": _sha(data), "bytes": len(data), "mime": _FILES[name]}:
            raise ReportResourceError("artifact_mismatch", f"Hash or size mismatch: {name}")
    provenance = _json(bundle / "provenance.json")
    for key in ("source_run_id", "source_runtime_id", "live_recovery_id", "assembly_id", "paper_id"):
        if not isinstance(manifest.get(key), str) or not manifest[key] or provenance.get(key) != manifest[key]:
            raise ReportResourceError("invalid_provenance", f"Missing or mismatched {key}")
    report = NoveltyReport.model_validate_json((bundle / "report.json").read_text())
    if report.paper_id != manifest["paper_id"]:
        raise ReportResourceError("identity_mismatch", "Report paper ID differs from provenance")
    audit = bundle / "audit.json"
    if audit.is_symlink() or not audit.is_file():
        raise ReportResourceError("missing_audit", "Trusted input audit is required")
    source = _json(audit)
    full_input_path = bundle / "synthesis-input.json"
    if full_input_path.is_symlink() or not full_input_path.is_file():
        raise ReportResourceError("missing_audit", "Original synthesis input is required")
    if _sha(full_input_path.read_bytes()) != manifest.get("source_input_sha256"):
        raise ReportResourceError("source_mismatch", "Original synthesis input hash differs")
    full_input = _json(full_input_path)
    for field in ("novelty_points", "novelty_reviews", "evidence_cards", "rejected_evidence"):
        if source.get(field, []) != full_input.get(field, []):
            raise ReportResourceError("source_mismatch", f"Audit projection differs: {field}")
    if source.get("paper_id") != report.paper_id:
        raise ReportResourceError("identity_mismatch", "Audit paper ID differs from report")
    source_run = source.get("source_run_manifest")
    if not isinstance(source_run, dict) or any((
        source_run.get("status") != manifest["source_run_status"],
        source_run.get("run_id") != manifest["source_runtime_id"],
        source_run.get("paper_id") != report.paper_id,
    )):
        raise ReportResourceError("invalid_provenance", "Original run manifest does not match")
    points = [NoveltyPoint.model_validate(item) for item in source["novelty_points"]]
    reviews = [NoveltyPointReview.model_validate(item) for item in source["novelty_reviews"]]
    cards = [EvidenceCard.model_validate(item) for item in source["evidence_cards"]]
    response_path = bundle / "live-response.json"
    if response_path.is_symlink() or not response_path.is_file():
        raise ReportResourceError("missing_response", "Captured live response is required")
    if _sha(response_path.read_bytes()) != manifest.get("live_response_sha256"):
        raise ReportResourceError("response_mismatch", "Captured response hash differs")
    response = _json(response_path)
    if response.get("status") != "SUCCESS" or response.get("finish_reason") != "stop":
        raise ReportResourceError("response_mismatch", "Captured response did not finish normally")
    draft = ReportNarrativeDraft.model_validate_json(response["response"]["content"])
    reproduced = assemble_report_from_draft(
        draft, paper_id=report.paper_id, novelty_points=points,
        novelty_reviews=reviews, evidence_cards=cards,
        rejected_evidence=source.get("rejected_evidence", []),
    )
    if reproduced != report:
        raise ReportResourceError("assembly_mismatch", "Report does not match the captured response")
    result = validate_report_integrity(
        report,
        novelty_points=points,
        novelty_reviews=reviews,
        evidence_cards=cards,
    )
    if not result.validation_passed:
        raise ReportResourceError("integrity_failed", "; ".join(result.issues))
    expected_ids = [point["point_id"] for point in source["novelty_points"]]
    if [item.novelty_point_id for item in report.conclusions] != expected_ids:
        raise ReportResourceError("point_order_mismatch", "Report point order differs from source")
    if manifest.get("source_input_sha256") != source.get("source_input_sha256"):
        raise ReportResourceError("source_mismatch", "Source input hash differs from audit")
    return manifest


class ReportResourceStore:
    def __init__(self, root: Path):
        self.root = root

    def register(self, bundle: Path, *, dry_run: bool = False) -> dict:
        try:
            manifest = verify_bundle(bundle)
        except ReportResourceError as exc:
            logger.warning("report_resource_registration_failed utc=%s code=%s bundle=%s",
                           datetime.now(timezone.utc).isoformat(), exc.code, bundle)
            raise
        identity = json.dumps({key: manifest[key] for key in (
            "source_run_id", "live_recovery_id", "assembly_id", "paper_id")}, sort_keys=True).encode()
        resource_id = "rr-" + _sha(identity)[:32]
        public = {key: value for key, value in manifest.items() if key != "source_input_sha256"}
        public.update(report_resource_id=resource_id, artifact_integrity="passed",
                      semantic_status="inherited_not_reassessed")
        target = self.root / resource_id
        if target.exists():
            current = self.get(resource_id)
            comparable = {key: value for key, value in current.items() if key != "registered_at"}
            expected = {key: value for key, value in public.items() if key != "registered_at"}
            if comparable != expected:
                logger.warning("report_resource_registration_conflict utc=%s id=%s source=%s",
                               datetime.now(timezone.utc).isoformat(), resource_id,
                               manifest["source_run_id"])
                raise ReportResourceError("resource_conflict", "Same identity has different content")
            return current
        if dry_run:
            return public
        self.root.mkdir(parents=True, exist_ok=True)
        index = self._index()
        temporary = self.root / (".staging-" + uuid.uuid4().hex)
        temporary.mkdir()
        try:
            for name in _FILES:
                shutil.copyfile(bundle / name, temporary / name)
            public["registered_at"] = datetime.now(timezone.utc).isoformat()
            manifest_bytes = (json.dumps(public, ensure_ascii=False, indent=2) + "\n").encode()
            (temporary / "manifest.json").write_bytes(manifest_bytes)
            os.replace(temporary, target)
            index[resource_id] = _sha(manifest_bytes)
            index_tmp = self.root / (".index-" + uuid.uuid4().hex)
            index_tmp.write_text(json.dumps(index, sort_keys=True, indent=2) + "\n")
            os.replace(index_tmp, self.root / "index.json")
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)
        logger.info("report_resource_registered id=%s source=%s", resource_id, manifest["source_run_id"])
        return public

    def _directory(self, resource_id: str) -> Path:
        if not _ID.fullmatch(resource_id):
            raise ReportResourceError("unknown_resource", "Unknown report resource")
        path = self.root / resource_id
        if path.is_symlink() or not path.is_dir() or path.resolve().parent != self.root.resolve():
            raise ReportResourceError("unknown_resource", "Unknown report resource")
        return path

    def _index(self) -> dict:
        path = self.root / "index.json"
        if not path.exists():
            return {}
        if path.is_symlink():
            raise ReportResourceError("invalid_index", "Linked report index")
        data = _json(path)
        if not isinstance(data, dict):
            raise ReportResourceError("invalid_index", "Report index is invalid")
        return data

    def get(self, resource_id: str) -> dict:
        directory = self._directory(resource_id)
        manifest_path = directory / "manifest.json"
        if manifest_path.is_symlink() or not manifest_path.is_file():
            raise ReportResourceError("invalid_manifest", "Missing or linked resource manifest")
        expected_hash = self._index().get(resource_id)
        if expected_hash is None:
            raise ReportResourceError("unknown_resource", "Resource is not registered")
        if _sha(manifest_path.read_bytes()) != expected_hash:
            raise ReportResourceError("invalid_manifest", "Resource manifest changed")
        manifest = _json(manifest_path)
        if manifest.get("report_resource_id") != resource_id or set(manifest.get("files", {})) != set(_FILES):
            raise ReportResourceError("invalid_manifest", "Resource manifest is invalid")
        for name in _FILES:
            self._read_verified(directory, manifest, name)
        return manifest

    def _read_verified(self, directory: Path, manifest: dict, name: str) -> bytes:
        path = directory / name
        if path.is_symlink() or not path.is_file() or path.resolve().parent != directory.resolve():
            raise ReportResourceError("missing_artifact", f"Missing or linked {name}")
        data = path.read_bytes()
        if manifest["files"].get(name) != {"sha256": _sha(data), "bytes": len(data), "mime": _FILES[name]}:
            raise ReportResourceError("artifact_mismatch", f"Published file changed: {name}")
        return data

    def content(self, resource_id: str, name: str = "report.md") -> tuple[dict, bytes]:
        if name not in _FILES:
            raise ReportResourceError("unknown_file", "File is not published")
        manifest = self.get(resource_id)
        return manifest, self._read_verified(self._directory(resource_id), manifest, name)
