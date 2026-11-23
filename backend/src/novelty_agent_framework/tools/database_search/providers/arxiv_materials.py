"""arXiv raw HTML → PDF acquisition. No body transcription occurs here."""
from __future__ import annotations

import asyncio
import re
from datetime import datetime, timezone
from urllib.parse import urlsplit, urljoin

from lxml import etree, html

from ....material_store import MaterialStore, digest
from ....persistence import _safe_storage_id
from ....schemas.materials import (
    AcquisitionAttempt, AcquisitionProvenance, MaterialAcquisitionRequest,
    MaterialAcquisitionResult, MaterialFormat, MaterialRef, RawMaterialArtifact,
    RawRecordBinding, canonical_material_json,
)
from ....schemas.failures import FailureCode, make_failure
from .arxiv_web import ArxivWebSession, ArxivWebChannelError
from ....core.runtime_artifacts import ProviderPhysicalBudgetExceeded

_IDENTIFIER = re.compile(r"(?P<base>(?:\d{4}\.\d{4,5}|[a-zA-Z][a-zA-Z.\-]*/\d{7}))(?P<version>v[1-9]\d*)?\Z")


class ArxivMaterialProvider:
    source_id = "arxiv"

    def __init__(self, store: MaterialStore, *, session: ArxivWebSession | None = None):
        self.store = store
        self.session = session or ArxivWebSession()

    async def acquire(self, request: MaterialAcquisitionRequest) -> MaterialAcquisitionResult:
        return await asyncio.to_thread(self.acquire_sync, request)

    def acquire_sync(self, request: MaterialAcquisitionRequest) -> MaterialAcquisitionResult:
        request = MaterialAcquisitionRequest.model_validate(request)
        if request.source_id != self.source_id:
            raise ValueError("request source_id does not match arxiv provider")
        attempts, issues = [], []

        def result(status, materials=()):
            return MaterialAcquisitionResult(schema_version=1, request_id=request.request_id,
                status=status, materials=list(materials), attempts=attempts, issues=issues)

        if request.requested_role != "full_text":
            return result("unsupported")
        target = request.target
        match = _IDENTIFIER.fullmatch(target.external_id)
        version = target.requested_version
        if (not match or (version is not None and not re.fullmatch(r"v[1-9]\d*", version))
                or (match.group("version") and version and match.group("version") != version)):
            issues.append(self._failure(request, FailureCode.PROVIDER_RESULT_INVALID, "Invalid or contradictory arXiv identity/version."))
            return result("failed")
        version = version or match.group("version")
        identifier = match.group("base") + (version or "")
        try:
            _safe_storage_id(target.work_id, "work_id")
            # Only a pinned version can be reused without checking upstream latest.
            if version:
                for raw in self.store.load(request.scope).raw:
                    if (raw.provenance.source_id == self.source_id and raw.role.value == "full_text"
                            and raw.format.family in request.format_preferences and raw.byte_size <= request.max_bytes
                            and any(b.source_record_id == target.source_record_id and b.work_id == target.work_id
                                    and b.external_id == target.external_id and b.observed_version == version
                                    for b in raw.bindings)):
                        _, body = self.store.raw(raw.ref)
                        self._cache_assets(raw, body, request, issues)
                        return result("acquired", [raw])
        except (OSError, ValueError):
            issues.append(self._failure(request, FailureCode.MATERIAL_INTEGRITY, "Existing material registry or file failed verification."))
            return result("failed")
        supported = False
        for family in request.format_preferences:
            if family.value not in {"html", "pdf"}:
                continue
            supported = True
            url = f"https://arxiv.org/{family.value}/{identifier}"
            events = []
            response = None
            fatal = None
            raw = None
            try:
                response = self.session.get(url, max_bytes=request.max_bytes, on_attempt=events.append)
                candidate = self._describe(request, family.value, identifier, version, response)
                raw = self.store.save_raw(candidate, response.content)
            except ProviderPhysicalBudgetExceeded:
                fatal = "failed"
                issues.append(self._failure(request, FailureCode.BUDGET_EXHAUSTED, "Provider physical request budget exhausted."))
            except ArxivWebChannelError:
                status = events[-1]["http_status"] if events else None
                if status in {401, 403}:
                    fatal = "unauthorized"
                    issues.append(self._failure(request, FailureCode.PROVIDER_AUTHORIZATION, "arXiv access denied; format fallback stopped."))
                elif status != 404:
                    fatal = "failed"
                    issues.append(self._failure(request, FailureCode.PROVIDER_NETWORK, "arXiv request failed; no raw material registered."))
            except (ValueError, UnicodeError):
                # Invalid HTTP 200 HTML is not a paper; fallback within the same identity is allowed.
                issues.append(self._failure(request, FailureCode.PROVIDER_RESULT_INVALID, "Response failed material type, body or identity validation."))
            except OSError:
                fatal = "failed"
                issues.append(self._failure(request, FailureCode.MATERIAL_INTEGRITY, "Raw material could not be persisted."))
            for event in events:
                http_status = event["http_status"]
                is_saved = raw is not None and response is not None and event["attempt_id"] == response.extensions["arxiv_request_id"]
                status = ("acquired" if is_saved else "not_found" if http_status == 404
                          else "unauthorized" if http_status in {401, 403}
                          else "unavailable" if http_status is not None and 300 <= http_status < 400
                          else "failed")
                attempt_issues = []
                if status == "failed":
                    attempt_issues = [self._failure(request, FailureCode.PROVIDER_RESULT_INVALID,
                                      "Physical request did not produce a registered material.", event["attempt_id"])]
                attempts.append(AcquisitionAttempt(attempt_id=event["attempt_id"], requested_format=family,
                    status=status, http_status=http_status, artifact_refs=[raw.ref] if is_saved else [], issues=attempt_issues))
            if raw is not None:
                self._cache_assets(raw, response.content, request, issues)
                return result("acquired", [raw])
            if fatal:
                return result(fatal)
        if not supported:
            return result("unsupported")
        return result("failed" if issues else "unavailable")

    def _cache_assets(self, raw, body, request, issues):
        if raw.format.family != "html":
            return
        try:
            tree = html.document_fromstring(body, parser=html.HTMLParser(no_network=True))
        except (ValueError, etree.ParserError):
            return
        base = raw.provenance.final_url
        paper_path = urlsplit(base).path
        if not re.fullmatch(r"/html/[a-zA-Z0-9./-]+v[1-9][0-9]*", paper_path):
            return
        remaining = min(request.max_bytes, 16 * 1024 * 1024)
        sources = dict.fromkeys(tree.xpath("//article//figure//img/@src"))
        for src in list(sources)[:12]:
            url = urljoin(base, src)
            parts = urlsplit(url)
            suffix = parts.path.removeprefix(paper_path + "/")
            if (parts.netloc != "arxiv.org" or parts.scheme != "https" or parts.query or parts.fragment
                    or not parts.path.startswith(paper_path + "/")
                    or not re.fullmatch(r"[A-Za-z0-9_-][A-Za-z0-9_./-]*\.(?:png|jpe?g)", suffix, re.I)
                    or ".." in suffix.split("/")):
                continue
            events = []
            try:
                if self.store.html_asset(raw, src) is not None:
                    continue
                if remaining <= 0:
                    break
                resp = self.session.get(url, max_bytes=min(remaining, 8*1024*1024), on_attempt=events.append)
                remaining -= len(resp.content)
                if str(resp.url) != url:
                    raise ValueError("asset source changed")
                image = resp.content
                if not image.startswith((b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff")):
                    raise ValueError("unexpected figure asset format")
                self.store.save_html_asset(raw, src, url, image)
            except (ArxivWebChannelError, ProviderPhysicalBudgetExceeded, OSError, ValueError) as exc:
                issues.append(self._failure(request, FailureCode.PROVIDER_RESULT_INVALID,
                                            "Supplementary image unavailable; raw HTML was retained."))
                if isinstance(exc, ValueError) or (events and events[-1].get("http_status") in {404,410}):
                    continue
                break

    def _describe(self, request, family, identifier, version, response):
        final = urlsplit(str(response.url))
        path_id = final.path.removeprefix(f"/{family}/").removesuffix(".pdf")
        match = _IDENTIFIER.fullmatch(path_id)
        expected = _IDENTIFIER.fullmatch(identifier)
        if (final.scheme != "https" or final.hostname != "arxiv.org" or final.query
                or not final.path.startswith(f"/{family}/") or not match
                or match.group("base") != expected.group("base")
                or (version and match.group("version") != version)):
            raise ValueError("response identity/version differs from requested arxiv resource")
        observed_version = match.group("version")
        content_type = response.headers.get("content-type", "")
        media_type = content_type.partition(";")[0].strip().lower()
        body = response.content
        if not body or response.status_code != 200:
            raise ValueError("empty or incomplete response")
        charset = None
        if family == "html":
            if media_type != "text/html":
                raise ValueError("not an HTML response")
            charset = response.encoding or "utf-8"
            html = body.decode(charset, errors="strict")
            if not re.search(r'<article\b[^>]*\bclass=["\'][^"\']*\bltx_document\b', html, re.I):
                raise ValueError("not an arxiv LaTeXML article")
            # The header precedes citations and may expose a version on unversioned URLs.
            header = html.split("<article", 1)[0]
            marker = re.search(r"arXiv:\s*([\w.\-/]+v[1-9]\d*)", header)
            if marker:
                observed = _IDENTIFIER.fullmatch(marker.group(1))
                if observed and (observed.group("base") != expected.group("base")
                                 or (version and observed.group("version") != version)):
                    raise ValueError("HTML header identity contradicts URL")
                if observed:
                    observed_version = observed.group("version")
        elif media_type != "application/pdf" or not body.startswith(b"%PDF-") or b"%%EOF" not in body[-2048:]:
            raise ValueError("not a complete PDF response")
        binding_data = dict(work_id=request.target.work_id, source_record_id=request.target.source_record_id,
                            external_id=request.target.external_id, observed_version=observed_version,
                            selector={"kind": "document"})
        binding = RawRecordBinding(**binding_data, binding_sha256=digest(canonical_material_json(binding_data)))
        sha256 = digest(body)
        identity = {"source_id": self.source_id, "body_sha256": sha256,
                    "family": family, "binding_sha256": binding.binding_sha256}
        artifact_id = "raw_" + digest(canonical_material_json(identity))[:24]
        ref = MaterialRef(**request.scope.model_dump(), artifact_id=artifact_id, sha256=sha256)
        return RawMaterialArtifact(
            schema_version=1, ref=ref, representation="raw", role="full_text",
            format=MaterialFormat(family=family, media_type=media_type, declared_content_type=content_type,
                                  charset=charset, schema_hint="arxiv_html" if family == "html" else None),
            relative_path=f"documents/{request.target.work_id}/raw/{artifact_id}.{family}",
            byte_size=len(body), transfer_complete=True, content_extent="unknown",
            provenance=AcquisitionProvenance(source_id=self.source_id, run_id=request.run_id,
                request_id=request.request_id, attempt_id=response.extensions["arxiv_request_id"],
                operation="full_text", view=None, request_url=f"https://arxiv.org/{family}/{identifier}",
                final_url=str(response.url), http_status=response.status_code, acquired_at=datetime.now(timezone.utc),
                content_encoding=response.headers.get("content-encoding"), etag=response.headers.get("etag"),
                last_modified=response.headers.get("last-modified")),
            bindings=[binding], warnings=[],
        )

    @staticmethod
    def _failure(request, code, message, occurrence=None):
        return make_failure(code, scope={"paper_id": request.scope.subject_paper_id, "run_id": request.run_id,
                                       "provider": "arxiv"}, message=message,
                            occurrence_id=occurrence or request.request_id)
