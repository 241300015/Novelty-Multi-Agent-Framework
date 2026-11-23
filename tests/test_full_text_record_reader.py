"""Exercise registered raw files through the same researcher entry as JSON abstracts."""
import asyncio

import httpx
import pytest

from novelty_agent_framework.schemas.materials import MaterialRef
from novelty_agent_framework.tools import ReferenceArtifactReaderTool, ReaderTool, ResearcherToolRegistry
from test_arxiv_materials import acquisition_tool, html_response, pdf_bytes, request
from test_record_reader import invoke
from test_reader_tool_call_harness_integration import research_scope


@pytest.fixture
def corpus(tmp_path):
    def serve(req):
        if "/pdf/" in req.url.path:
            return httpx.Response(200, content=pdf_bytes(), headers={"content-type": "application/pdf"})
        return html_response(req)
    retrieval, query = acquisition_tool(tmp_path, serve, full_text_limit=1)
    record = asyncio.run(retrieval.ainvoke(query)).source_records[0]
    acquisition = request(target={"work_id": record.work_id, "source_record_id": record.source_record_id,
                          "external_id": record.external_id, "requested_version": "v1"}, format_preferences=["pdf"])
    pdf = retrieval.material_provider.acquire_sync(acquisition).materials[0]
    catalog = retrieval.material_store.raw_catalog_artifact(pdf.ref, record.source_record_id)
    retrieval.reference_store.merge_manifest("paper-1", artifacts=[catalog])
    scope = research_scope().model_copy(update={"subject_paper_id": "paper-1"})
    def call(**args):
        registry = ResearcherToolRegistry([ReaderTool(ReferenceArtifactReaderTool(retrieval.reference_store))])
        return invoke(registry, scope, **args)
    return call, record, retrieval.material_store


def open_format(call, record, family=None):
    return call(operation="open", part="full_text", source_record_id=record.source_record_id,
                **({"source_format": family} if family else {}))


@pytest.mark.parametrize(("family", "expected", "kind"), [
    ("html", "Method uses a graph.", "html_xpath"), ("pdf", "Raw PDF document text.", "pdf_page"),
])
def test_format_pages_and_quote(corpus, family, expected, kind):
    call, record, _ = corpus
    opened = open_format(call, record, family)
    assert opened["status"] == "opened" and opened["source_format"] == family
    assert open_format(call, record)["source_format"] == "html"
    args = dict(operation="read", handle=opened["handle"], max_tokens=7)
    blocks, page = [], call(**args)
    while True:
        assert page["status"] == "content"
        assert sum(len(b["content"].encode()) for b in page["blocks"]) <= 7
        blocks.extend(page["blocks"])
        if page["next_cursor"] is None:
            break
        page = call(**args, cursor=page["next_cursor"])
    assert expected in "".join(b["content"] for b in blocks)
    assert all(b["locator"]["origin"]["kind"] == kind for b in blocks)
    quoted = next(b for b in blocks if b["content"].strip())
    citation = {k: quoted[k] for k in ("locator", "block_id")}
    assert call(**args, citation=citation)["blocks"] == [quoted]
    assert call(**args, citation=dict(citation, block_id="0"*64))["status"] == "invalid_address"
    other = open_format(call, record, "pdf" if family == "html" else "html")
    assert call(operation="read", handle=other["handle"], citation=citation)["status"] == "invalid_address"
    section = opened["outline"][0]["section_id"]
    assert call(**args, section_id=section)["status"] == "content"
    assert call(**args, section_id="missing")["status"] == "invalid_address"


def test_tampered_raw_after_open(corpus):
    call, record, materials = corpus
    handle = open_format(call, record, "pdf")["handle"]
    ref = MaterialRef(subject_paper_id=handle["subject_paper_id"], namespace=handle["namespace"],
                      artifact_id=handle["raw_id"], sha256=handle["sha256"])
    raw, body = materials.raw(ref)
    materials._path(ref, raw.relative_path).write_bytes(body + b"!")
    assert call(operation="read", handle=handle)["status"] == "integrity_error"


@pytest.mark.parametrize("failure", ["encoding", "depth"])
def test_unreadable_html_falls_back_to_same_version_pdf(corpus, monkeypatch, failure):
    from novelty_agent_framework.tools import material_reader
    call, record, _ = corpus
    extract = material_reader._html_segments
    def malformed(body, charset):
        if failure == "encoding":
            return extract(body, "not-a-charset")
        return extract(b'<article class="ltx_document">prefix' + b"<div>" * 300 +
                       b"missing result" + b"</div>" * 300 + b"</article>", charset)
    monkeypatch.setattr(material_reader, "_html_segments", malformed)
    assert open_format(call, record, "html")["status"] == "parse_failed"
    opened = open_format(call, record)
    assert opened["source_format"] == "pdf"
    assert any("html: failed" in w for w in opened["warnings"])


def test_many_short_nodes_have_bounded_pages(tmp_path):
    from test_arxiv_materials import web
    web.reset_shared_arxiv_web_gate()
    content = b'<article class="ltx_document">' + b"<section>x</section>" * 310 + b"</article>"
    def handler(req):
        return httpx.Response(200, content=content, headers={"content-type": "text/html; charset=utf-8"})
    retrieval, query = acquisition_tool(tmp_path, handler, full_text_limit=1)
    record = asyncio.run(retrieval.ainvoke(query)).source_records[0]
    reg = ResearcherToolRegistry([ReaderTool(ReferenceArtifactReaderTool(retrieval.reference_store))])
    scope = research_scope().model_copy(update={"subject_paper_id": "paper-1"})
    opened = invoke(reg, scope, operation="open", source_record_id=record.source_record_id, part="full_text")
    assert len(opened["outline"]) == 100
    args = dict(operation="read", handle=opened["handle"], max_tokens=640)
    page = invoke(reg, scope, **args)
    pieces = []
    while True:
        assert 1 <= len(page["blocks"]) <= 16
        pieces.extend(b["content"] for b in page["blocks"])
        if page["next_cursor"] is None:
            break
        page = invoke(reg, scope, **args, cursor=page["next_cursor"])
    assert "".join(pieces) == "x\n" * 310


@pytest.mark.parametrize("target", ["parsed", "map"])
def test_corrupted_cache_cannot_trigger_format_fallback(corpus, target):
    call, record, materials = corpus
    handle = open_format(call, record, "html")["handle"]
    ref = MaterialRef(subject_paper_id=handle["subject_paper_id"], namespace=handle["namespace"],
                      artifact_id=handle["parsed_id"], sha256=handle["parsed_sha256"])
    parsed, _, _ = materials.parsed(ref)
    relative_path = parsed.relative_path if target == "parsed" else parsed.source_map.relative_path
    materials._path(ref, relative_path).write_bytes(b"corrupted")
    assert open_format(call, record)["status"] == "integrity_error"
    assert open_format(call, record, "html")["status"] == "integrity_error"
    assert call(operation="read", handle=handle)["status"] == "integrity_error"


def test_candidate_full_text_returns_replayable_evidence(corpus):
    _, rec, materials = corpus
    registry = ResearcherToolRegistry([ReaderTool(ReferenceArtifactReaderTool(materials.references))])
    scope = research_scope().model_copy(update={"subject_paper_id": "paper-1"})
    candidate = {"source_record_id": rec.source_record_id, "part": "full_text"}
    obs = asyncio.run(registry.execute("reader", {"candidate": candidate}, scope=scope))
    assert obs.succeeded, obs.error
    read = registry.project_model_context("reader", obs)["read_result"]
    assert "Method uses a graph." in read["text"]
    page = obs.payload["candidate_page"]
    restored = [invoke(registry, scope, operation="read", handle=page["handle"], citation=c)["blocks"][0]
                for c in page["citations"]]
    assert "".join(b["content"] for b in restored) == read["text"]
    evidence = materials.references.read_document_slice("paper-1", artifact_id=read["artifact_id"],
                          char_start=read["char_start"], max_chars=read["char_end"] - read["char_start"])
    assert evidence.text == read["text"]
