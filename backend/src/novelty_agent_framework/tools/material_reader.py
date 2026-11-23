"""Offline arXiv HTML/PDF parsing, immutable read representations and source maps."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from lxml import etree, html

import pymupdf

from ..material_store import MaterialStore, digest
from ..schemas.materials import (
    IndexFileRef, MaterialOpenRequest, MaterialOpenResult, MaterialReadRequest,
    MaterialReadResult, MaterialReadSlice, MaterialRef, ParsedMaterialArtifact,
    ParserIdentity, SourceMap, SourceMapping, canonical_material_json,
)
from ..schemas.failures import FailureCode, make_failure
from ..persistence import _safe_storage_id


def _html_article(body, charset):
    """Extract text in document order and retain structural origins from the original DOM."""
    try:
        parser = html.HTMLParser(encoding=charset or "utf-8", no_network=True)
        tree = html.document_fromstring(body, parser=parser)
    except (LookupError, etree.ParserError) as exc:
        raise ValueError("invalid article encoding or markup") from exc
    if any(event.level_name == "FATAL" for event in parser.error_log):
        raise ValueError("HTML parser could not process the complete document")
    articles = tree.xpath("//article[contains(concat(' ', normalize-space(@class), ' '), ' ltx_document ')]")
    if len(articles) != 1:
        raise ValueError("expected one arXiv article")
    return articles[0]


def _html_segments(body, charset):
    article = _html_article(body, charset)
    dom = article.getroottree()
    # Capture paths before removing navigation or replacing equation markup.
    paths = {child: dom.getpath(child) for child in article if isinstance(child.tag, str)}
    root_path = dom.getpath(article)
    warnings = []
    if article.xpath(".//table"):
        warnings.append("Tables are rendered as rows of text; merged cells and visual layout are not preserved.")
    for node in article.xpath(".//nav | .//style | .//script | .//comment()"):
        if isinstance(node, html.HtmlElement):
            node.drop_tree()
    for node in article.xpath(".//math[@alttext] | .//img"):
        if node.tag == "img":
            warnings.append("Image pixels are not represented; only alt text and captions are readable.")
        replacement = node.get("alttext") if node.tag == "math" else node.get("alt", "")
        for child in list(node):
            node.remove(child)
        node.text = " " + replacement + " "
    for node in article.iterdescendants():
        if node.tag in {"p", "div", "section", "li", "tr", "br", "h1", "h2", "h3", "h4", "h5", "h6"}:
            node.tail = "\n" + (node.tail or "")
        elif node.tag in {"td", "th"}:
            node.tail = "\t" + (node.tail or "")
    segments = []
    def append(text, path):
        if text:
            segments.append((text, {"kind": "html_xpath", "value": path}, "structural"))
    append(article.text, root_path)
    for child in article:
        if child in paths:
            append(child.text_content(), paths[child])
        append(child.tail, root_path)
    return segments, list(dict.fromkeys(warnings))


class LocalMaterialReader:
    parser_version = "1"

    def __init__(self, store: MaterialStore):
        self.store = store

    @classmethod
    def parser_for(cls, raw, max_output_chars):
        family = raw.format.family.value
        if family == "html" and raw.format.schema_hint == "arxiv_html":
            name = "arxiv_html_article"
        elif family == "pdf" and raw.format.media_type == "application/pdf":
            name = "pymupdf_pages"
        else:
            return None
        config = {"max_output_chars": max_output_chars, "normalization": "article-blocks-v1",
                  "ocr": False, "pymupdf_version": pymupdf.VersionBind if family == "pdf" else None}
        if family == "html":
            config.update(normalization="article-xpath-v3", lxml_version=etree.LXML_VERSION,
                          libxml_version=etree.LIBXML_VERSION)
        return ParserIdentity(name=name, version=cls.parser_version,
                                config_sha256=digest(canonical_material_json(config)))

    @staticmethod
    def parsed_id(raw, binding, parser):
        # The selection is stable before parsing. MaterialRef.sha256 separately
        # pins the actual output bytes, which the store verifies on every replay.
        identity = {"source_ref": raw.ref.model_dump(mode="json"), "binding_sha256": binding.binding_sha256,
                    "parser": parser.model_dump(mode="json")}
        return "parsed_" + digest(canonical_material_json(identity))[:24]

    async def open(self, request: MaterialOpenRequest) -> MaterialOpenResult:
        return await asyncio.to_thread(self.open_sync, request)

    def open_sync(self, request: MaterialOpenRequest) -> MaterialOpenResult:
        request = MaterialOpenRequest.model_validate(request)
        try:
            raw, body = self.store.raw(request.raw_ref)
            binding = next((b for b in raw.bindings if b.source_record_id == request.source_record_id
                            and b.binding_sha256 == request.binding_sha256), None)
            if binding is None:
                raise ValueError("raw binding does not match open request")
            if binding.selector.kind != "document" or raw.role.value != "full_text":
                return self._open_result(request, "unsupported")
            family = raw.format.family.value
            parser = self.parser_for(raw, request.max_output_chars)
            if parser is None:
                return self._open_result(request, "unsupported")
            for item in self.store.load(self.store.scope(raw.ref)).parsed:
                if (item.source_ref == raw.ref and item.source_record_id == binding.source_record_id
                        and item.binding_sha256 == binding.binding_sha256 and item.parser == parser):
                    item, _, _ = self.store.parsed(item.ref)
                    return self._open_result(request, "partial" if item.content_extent.value == "partial" else "ready", item)
            try:
                warnings = []
                segments = []
                if family == "html":
                    segments, warnings = _html_segments(body, raw.format.charset)
                else:
                    with pymupdf.open(stream=body, filetype="pdf") as document:
                        if document.needs_pass:
                            return self._open_result(request, "unsupported")
                        for page in document:
                            text = page.get_text(sort=True)
                            if not text.strip():
                                warnings.append(f"Page {page.number + 1} has no extractable text; OCR is disabled.")
                            if page.get_images():
                                warnings.append(f"Page {page.number + 1} contains images not represented in text.")
                            if text:
                                segments.append((text, {"kind": "pdf_page", "page": page.number + 1}, "structural"))
                text = "".join(segment[0] for segment in segments)
                if not text.strip():
                    raise ValueError("parser produced no readable text")
                if len(text) > request.max_output_chars:
                    warnings.append("Parsed text truncated at max_output_chars; raw source remains complete.")
                text = text[:request.max_output_chars]
                if not text.strip():
                    raise ValueError("output limit leaves no readable text")
            except (ValueError, RuntimeError) as exc:
                issue = self._failure(request, "open", type(exc).__name__, FailureCode.READER_DECODE_FAILED)
                return self._open_result(request, "failed", issues=[issue])
            artifact_id = self.parsed_id(raw, binding, parser)
            ref = MaterialRef(**self.store.scope(raw.ref).model_dump(), artifact_id=artifact_id,
                              sha256=digest(text.encode("utf-8")))
            entries = []
            start = 0
            for part, locator, precision in segments:
                end = min(start + len(part), len(text))
                if end > start:
                    entries.append(SourceMapping(parsed_char_start=start, parsed_char_end=end,
                                                 locator=locator, precision=precision))
                start = end
                if start == len(text):
                    break
            mapping = SourceMap(schema_version=1, parsed_ref=ref, source_ref=raw.ref, entries=entries)
            map_bytes = canonical_material_json(mapping.model_dump(mode="json")) + b"\n"
            work_id = _safe_storage_id(binding.work_id, "work_id")
            stem = f"documents/{work_id}/parsed/{artifact_id}"
            item = ParsedMaterialArtifact(
                schema_version=1, representation="parsed", ref=ref, source_ref=raw.ref,
                work_id=work_id, source_record_id=binding.source_record_id, binding_sha256=binding.binding_sha256,
                role=raw.role.value, parser=parser, media_type="text/plain", relative_path=stem + ".txt",
                byte_size=len(text.encode("utf-8")), char_count=len(text),
                content_extent="partial" if warnings else raw.content_extent,
                source_map=IndexFileRef(schema_version=1, relative_path=stem + ".map.json",
                                       sha256=digest(map_bytes), byte_size=len(map_bytes)),
                created_at=datetime.now(timezone.utc), warnings=warnings,
            )
            item = self.store.save_parsed(item, text, mapping)
            return self._open_result(request, "partial" if item.content_extent.value == "partial" else "ready", item)
        except (OSError, ValueError, RuntimeError) as exc:
            return self._open_result(request, "failed", issues=[self._failure(request, "open", type(exc).__name__)])

    async def read(self, request: MaterialReadRequest) -> MaterialReadResult:
        return await asyncio.to_thread(self.read_sync, request)

    def read_sync(self, request: MaterialReadRequest) -> MaterialReadResult:
        request = MaterialReadRequest.model_validate(request)
        try:
            item, text, mapping = self.store.parsed(request.parsed_ref)
            if request.char_start > len(text):
                raise ValueError("read start exceeds parsed text length")
            start = request.char_start
            end = min(len(text), start + request.max_chars)
            origins = []
            for entry in mapping.entries:
                left, right = max(start, entry.parsed_char_start), min(end, entry.parsed_char_end)
                if left < right:
                    payload = entry.model_dump(mode="json")
                    payload.update(parsed_char_start=left, parsed_char_end=right)
                    if entry.precision == "exact" and (left, right) != (entry.parsed_char_start, entry.parsed_char_end):
                        payload["precision"] = "structural"
                    origins.append(SourceMapping.model_validate(payload))
            identity = {"parsed_ref": item.ref.model_dump(mode="json"), "source_map_sha256": item.source_map.sha256,
                        "char_start": start, "char_end": end}
            part = MaterialReadSlice(
                read_id="read_" + digest(canonical_material_json(identity))[:24], parsed_ref=item.ref,
                source_ref=item.source_ref, source_record_id=item.source_record_id, work_id=item.work_id,
                role=item.role, media_type=item.media_type, char_start=start, char_end=end,
                text=text[start:end], has_more=end < len(text), origins=origins,
            )
            return MaterialReadResult(schema_version=1, request_id=request.request_id,
                                      status="content" if part.text else "eof", read=part, issues=[])
        except (OSError, ValueError, RuntimeError) as exc:
            return MaterialReadResult(schema_version=1, request_id=request.request_id, status="failed", read=None,
                                      issues=[self._failure(request, "read", type(exc).__name__)])

    @staticmethod
    def _open_result(request, status, document=None, issues=None):
        return MaterialOpenResult(schema_version=1, request_id=request.request_id, status=status,
                                  document=document, issues=issues or [])

    @staticmethod
    def _failure(request, operation, error_type, code=FailureCode.READER_VERIFICATION_FAILED):
        ref = request.raw_ref if operation == "open" else request.parsed_ref
        return make_failure(code,
                            scope={"paper_id": ref.subject_paper_id, "run_id": request.run_id,
                                   "artifact_id": ref.artifact_id}, occurrence_id=request.request_id,
                            message=f"Material {operation} failed ({error_type}); no successful content returned.")
