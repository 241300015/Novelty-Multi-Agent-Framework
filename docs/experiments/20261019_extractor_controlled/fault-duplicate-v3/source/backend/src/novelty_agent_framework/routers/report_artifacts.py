"""Read published recovery reports without loading the workflow service."""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response

from ..services.report_resources import ReportResourceError, ReportResourceStore

logger = logging.getLogger("uvicorn.error.report_resources")
router = APIRouter(prefix="/report-artifacts", tags=["report-artifacts"])


def _store(request: Request) -> ReportResourceStore:
    return request.app.state.report_resources


def _error(exc: ReportResourceError) -> HTTPException:
    status = 404 if exc.code in {"unknown_resource", "missing_artifact"} else 409
    return HTTPException(status_code=status, detail={"code": exc.code, "message": str(exc)})


def _log(resource_id: str, source: str | None, result: str, digest: str | None = None) -> None:
    logger.info("report_resource_read utc=%s request_id=%s resource_id=%s source_run_id=%s result=%s sha256=%s",
                datetime.now(timezone.utc).isoformat(), uuid.uuid4().hex,
                resource_id, source or "unknown", result, digest or "unknown")


@router.get("/{resource_id}")
def get_resource(resource_id: str, request: Request) -> dict:
    try:
        item = _store(request).get(resource_id)
    except ReportResourceError as exc:
        _log(resource_id, None, exc.code)
        raise _error(exc) from exc
    _log(resource_id, item["source_run_id"], "passed", item["files"]["report.md"]["sha256"])
    return item


def _content(resource_id: str, request: Request, *, name: str, download: bool = False) -> Response:
    try:
        item, data = _store(request).content(resource_id, name)
    except ReportResourceError as exc:
        _log(resource_id, None, exc.code)
        raise _error(exc) from exc
    filename = f"recovery-report-{resource_id}.md" if name == "report.md" else "provenance.json"
    disposition = "attachment" if download else "inline"
    headers = {"Content-Disposition": f'{disposition}; filename="{filename}"',
               "X-Content-Type-Options": "nosniff", "Cache-Control": "no-store"}
    _log(resource_id, item["source_run_id"], "passed", item["files"][name]["sha256"])
    return Response(data, media_type=item["files"][name]["mime"], headers=headers)


@router.get("/{resource_id}/content")
def get_content(resource_id: str, request: Request) -> Response:
    return _content(resource_id, request, name="report.md")


@router.get("/{resource_id}/download")
def download_content(resource_id: str, request: Request) -> Response:
    return _content(resource_id, request, name="report.md", download=True)


@router.get("/{resource_id}/provenance")
def get_provenance(resource_id: str, request: Request) -> Response:
    return _content(resource_id, request, name="provenance.json")
