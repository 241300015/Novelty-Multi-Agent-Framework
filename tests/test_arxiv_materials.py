"""Raw-byte acquisition, offline parsing and the existing evidence-reader bridge."""
import asyncio
import gzip

import httpx
import pymupdf
import pytest

from novelty_agent_framework.material_store import MaterialStore, digest
from novelty_agent_framework.persistence import ReferenceStore
from novelty_agent_framework.schemas import ReferenceReadRequest
from novelty_agent_framework.schemas.materials import (
    MaterialAcquisitionRequest, MaterialOpenRequest, MaterialReadRequest,
)
from novelty_agent_framework.tools.database_search.providers.arxiv_materials import ArxivMaterialProvider
from novelty_agent_framework.tools.database_search.providers import arxiv_web as web
from novelty_agent_framework.tools.material_reader import LocalMaterialReader
from novelty_agent_framework.tools.reference_reader import ReferenceArtifactReaderTool

HTML = b'''<!doctype html><html><head><title>A paper</title></head><body>
<nav>MENU MUST NOT ENTER EVIDENCE</nav><article class="ltx_document">
<h1>A paper</h1><section><p>Method uses a graph.</p><p>Second paragraph.</p>
<math alttext="x^2"><mi>duplicate math</mi></math><script>UNTRUSTED SCRIPT</script>
</section></article></body></html>'''


@pytest.fixture(autouse=True)
def gate():
    web.reset_shared_arxiv_web_gate()
    yield
    web.reset_shared_arxiv_web_gate()


def request(**updates):
    data = dict(schema_version=1, request_id="acquire-test", run_id="run-test",
        scope={"subject_paper_id": "paper-1", "namespace": "research_reference"},
        source_id="arxiv", target={"work_id": "wrk_test", "source_record_id": "src_test",
                                  "external_id": "2402.03300v1", "requested_version": "v1"},
        requested_role="full_text", format_preferences=["html", "pdf"], max_bytes=1_000_000)
    data.update(updates)
    return MaterialAcquisitionRequest(**data)


def provider(tmp_path, handler, **options):
    session = web.ArxivWebSession(client=httpx.Client(transport=httpx.MockTransport(handler)),
                                 min_interval=0, max_retries=0, **options)
    store = MaterialStore(ReferenceStore(tmp_path))
    return ArxivMaterialProvider(store, session=session)


def html_response(req):
    return httpx.Response(200, content=HTML, headers={"content-type": "text/html; charset=utf-8"})


def open_request(raw, **updates):
    data = dict(schema_version=1, request_id="open-test", run_id="run-test", raw_ref=raw.ref,
                source_record_id=raw.bindings[0].source_record_id, binding_sha256=raw.bindings[0].binding_sha256,
                max_output_chars=10_000)
    data.update(updates)
    return MaterialOpenRequest(**data)


def read_request(parsed, **updates):
    data = dict(schema_version=1, request_id="read-test", run_id="run-test",
                parsed_ref=parsed.ref, char_start=0, max_chars=16000)
    data.update(updates)
    return MaterialReadRequest(**data)


def pdf_bytes():
    with pymupdf.open() as doc:
        page = doc.new_page()
        page.insert_text((72, 72), "Raw PDF document text.")
        return doc.tobytes()


def test_raw_html_is_saved_exactly_and_reader_is_offline(tmp_path):
    calls = []
    def handler(req):
        calls.append(str(req.url))
        return html_response(req)
    tool = provider(tmp_path, handler)
    result = asyncio.run(tool.acquire(request()))
    assert result.status == "acquired"
    raw = result.materials[0]
    assert raw.relative_path.startswith("documents/wrk_test/raw/")
    assert tool.store.raw(raw.ref)[1] == HTML
    assert raw.ref.sha256 == digest(HTML)
    assert len(result.attempts) == 1 and result.attempts[0].artifact_refs == [raw.ref]
    reader = LocalMaterialReader(tool.store)
    opened = asyncio.run(reader.open(open_request(raw)))
    assert opened.status == "ready"
    parsed = opened.document
    assert parsed.relative_path.startswith("documents/wrk_test/parsed/")
    read = asyncio.run(reader.read(read_request(parsed)))
    assert "Method uses a graph." in read.read.text
    assert "x^2" in read.read.text
    assert "MENU MUST" not in read.read.text and "UNTRUSTED SCRIPT" not in read.read.text
    assert "duplicate math" not in read.read.text
    assert calls == ["https://arxiv.org/html/2402.03300v1"]
    assert tool.store.raw(raw.ref)[1] == HTML
    again = asyncio.run(tool.acquire(request(request_id="second-acquire")))
    assert again.materials[0] == raw and again.attempts == []
    assert asyncio.run(reader.open(open_request(raw))).document == parsed
    assert len(tool.store.load(request().scope).parsed) == 1
    eof = reader.read_sync(read_request(parsed, char_start=parsed.char_count))
    assert eof.status == "eof" and eof.read.text == "" and not eof.read.origins
    assert reader.read_sync(read_request(parsed, char_start=parsed.char_count + 1)).status == "failed"


def test_pdf_fallback_preserves_version_and_bytes(tmp_path):
    pdf = pdf_bytes()
    calls = []
    def handler(req):
        calls.append(str(req.url))
        return httpx.Response(404) if "/html/" in str(req.url) else httpx.Response(
            200, content=pdf, headers={"content-type": "application/pdf"})
    tool = provider(tmp_path, handler)
    result = tool.acquire_sync(request())
    assert [a.status for a in result.attempts] == ["not_found", "acquired"]
    raw = result.materials[0]
    assert calls == ["https://arxiv.org/html/2402.03300v1", "https://arxiv.org/pdf/2402.03300v1"]
    assert tool.store.raw(raw.ref)[1] == pdf
    parsed = LocalMaterialReader(tool.store).open_sync(open_request(raw)).document
    _, text, mapping = tool.store.parsed(parsed.ref)
    assert "Raw PDF document text." in text
    assert mapping.entries[0].locator.kind == "pdf_page"
    assert mapping.entries[0].locator.page == 1


@pytest.mark.parametrize("status", [401, 403])
def test_permission_denial_stops_fallback(tmp_path, status):
    calls = []
    tool = provider(tmp_path, lambda req: calls.append(str(req.url)) or httpx.Response(status))
    result = tool.acquire_sync(request())
    assert result.status == "unauthorized" and result.materials == []
    assert len(calls) == 1
    assert not tool.store.load(request().scope).raw


def test_login_html_cannot_be_registered_as_full_text(tmp_path):
    tool = provider(tmp_path, lambda req: httpx.Response(200, text="<html>Sign in</html>",
                                                       headers={"content-type": "text/html"}))
    result = tool.acquire_sync(request())
    assert result.status == "failed" and result.materials == []
    assert not tool.store.load(request().scope).raw


def test_version_changing_redirect_is_not_saved(tmp_path):
    def handler(req):
        if str(req.url).endswith("v1"):
            return httpx.Response(302, headers={"location": str(req.url).replace("v1", "v2")})
        return html_response(req)
    result = provider(tmp_path, handler).acquire_sync(request(format_preferences=["html"]))
    assert result.status == "failed" and not result.materials
    assert len(result.attempts) == 2


def test_limit_applies_to_decoded_body_not_gzip_size(tmp_path):
    body = HTML + b" " * 10000
    tool = provider(tmp_path, lambda req: httpx.Response(200, content=gzip.compress(body),
        headers={"content-type": "text/html", "content-encoding": "gzip"}))
    result = tool.acquire_sync(request(max_bytes=1000))
    assert result.status == "failed" and not result.materials
    assert not tool.store.load(request().scope).raw


def test_http_gzip_is_decoded_once_before_persisting(tmp_path):
    tool = provider(tmp_path, lambda req: httpx.Response(200, content=gzip.compress(HTML),
        headers={"content-type": "text/html", "content-encoding": "gzip"}))
    raw = tool.acquire_sync(request()).materials[0]
    assert tool.store.raw(raw.ref)[1] == HTML
    assert raw.provenance.content_encoding == "gzip"


def test_parse_truncation_does_not_truncate_raw_and_changes_parser_identity(tmp_path):
    tool = provider(tmp_path, html_response)
    raw = tool.acquire_sync(request()).materials[0]
    reader = LocalMaterialReader(tool.store)
    partial = reader.open_sync(open_request(raw, max_output_chars=20))
    full = reader.open_sync(open_request(raw))
    assert partial.status == "partial" and partial.document.char_count == 20
    assert partial.document.ref.artifact_id != full.document.ref.artifact_id
    assert tool.store.raw(raw.ref)[1] == HTML


@pytest.mark.parametrize("which", ["raw", "parsed", "map"])
def test_tampering_any_file_prevents_reads_and_cache_reuse(tmp_path, which):
    tool = provider(tmp_path, html_response)
    raw = tool.acquire_sync(request()).materials[0]
    reader = LocalMaterialReader(tool.store)
    parsed = reader.open_sync(open_request(raw)).document
    relative = raw.relative_path if which == "raw" else parsed.relative_path if which == "parsed" else parsed.source_map.relative_path
    path = tmp_path / "paper-1" / "references" / relative
    path.write_bytes(path.read_bytes() + b"tampered")
    assert reader.read_sync(read_request(parsed)).status == "failed"
    assert reader.open_sync(open_request(raw)).status == "failed"


def test_registry_cannot_retarget_files_outside_scope(tmp_path):
    tool = provider(tmp_path, html_response)
    raw = tool.acquire_sync(request()).materials[0]
    root = tmp_path / "paper-1" / "references"
    target = root / raw.relative_path
    outside = tmp_path / "outside.html"
    outside.write_bytes(HTML)
    target.unlink()
    target.symlink_to(outside)
    assert LocalMaterialReader(tool.store).open_sync(open_request(raw)).status == "failed"


def test_parse_failure_keeps_raw_and_does_not_register_empty_text(tmp_path):
    tool = provider(tmp_path, lambda req: httpx.Response(200, text='<article class="ltx_document"></article>',
                                                       headers={"content-type": "text/html"}))
    raw = tool.acquire_sync(request()).materials[0]
    result = LocalMaterialReader(tool.store).open_sync(open_request(raw))
    assert result.status == "failed"
    assert tool.store.raw(raw.ref)[1]
    assert tool.store.load(request().scope).parsed == []


def acquisition_tool(tmp_path, handler=html_response, *, versions=("v1",), full_text_limit=0):
    from test_structured_retrieval_tool import request as retrieval_request
    from novelty_agent_framework.ports import SearchHit
    from novelty_agent_framework.tools.database_search import RetrievalSource, StructuredSourceRetrievalTool
    from novelty_agent_framework.tools.database_search.providers.arxiv import ArxivQueryAdapter
    class Search:
        source_id = "arxiv"
        def search(self, query, *, limit):
            return [SearchHit(document_id="2402.03300", external_id="2402.03300"+version, title="A paper",
                              abstract="A search abstract.", source_id="arxiv") for version in versions]
    configured = provider(tmp_path, handler)
    store = configured.store.references
    source = RetrievalSource(source_id="arxiv", query_adapter=ArxivQueryAdapter(), search_tool=Search(),
        material_provider_factory=lambda materials: ArxivMaterialProvider(materials, session=configured.session))
    retrieval = StructuredSourceRetrievalTool(source=source, reference_store=store, full_text_limit=full_text_limit)
    return retrieval, retrieval_request(source_id="arxiv")


@pytest.mark.parametrize("versions", [["v1"], ["v1", "v2"]])
@pytest.mark.parametrize("full_text_limit", [0, 8], ids=["on-demand", "eager"])
def test_acquisition_persists_json_abstracts_and_raw_files_without_reader(tmp_path, versions, full_text_limit, monkeypatch):
    from novelty_agent_framework.schemas import ResearchBundle
    def forbidden(*args, **kwargs):
        raise AssertionError("acquisition must not construct a Reader")
    monkeypatch.setattr(LocalMaterialReader, "__init__", forbidden)
    retrieval, req = acquisition_tool(tmp_path, versions=versions, full_text_limit=full_text_limit)
    discovered = asyncio.run(retrieval.ainvoke(req))
    if full_text_limit == 0:
        assert retrieval.material_store.load(request().scope).raw == []
        assert not list(tmp_path.rglob("*.txt"))
        result = asyncio.run(retrieval.acquire_full_texts(req, [r.source_record_id for r in discovered.source_records]))
    else:
        result = discovered
    materials = retrieval.material_store.load(request().scope)
    assert len(materials.raw) == len(versions)
    assert {a.artifact_id for a in result.artifacts} == {raw.ref.artifact_id for raw in materials.raw}
    assert all(a.role.value == "full_text" for a in result.artifacts)
    assert len(result.works) == 1
    assert all(r.abstract == "A search abstract." for r in result.source_records)
    assert all(r.access_status.value == "full_text_acquired" for r in result.source_records)
    assert ResearchBundle.model_validate_json(result.model_dump_json()) == result
    materials = retrieval.material_store.load(request().scope)
    assert materials.parsed == []
    for raw in materials.raw:
        assert retrieval.material_store.raw(raw.ref)[1] == HTML
        assert raw.relative_path.startswith(f"documents/{result.works[0].work_id}/raw/")
    manifest = retrieval.reference_store.load_manifest("paper-1")
    assert len(manifest.artifacts) == len(versions)
    assert all(a.role.value == "full_text" for a in manifest.artifacts)
    assert not list(tmp_path.rglob("*.txt"))
    assert not list(tmp_path.rglob("*.map.json"))
    assert not list(tmp_path.rglob("parsed"))
    # No corruption of the registry on a repeated pinned-version acquisition.
    again = asyncio.run(retrieval.acquire_full_texts(req, [r.source_record_id for r in result.source_records]))
    assert {a.artifact_id for a in again.artifacts} == {a.artifact_id for a in result.artifacts}
    assert {r.ref.artifact_id for r in retrieval.material_store.load(request().scope).raw} == {r.ref.artifact_id for r in materials.raw}
    assert len(retrieval.material_store.load(request().scope).raw) == len(versions)


def test_acquisition_failure_keeps_json_abstract_and_does_not_invent_artifacts(tmp_path):
    retrieval, req = acquisition_tool(tmp_path, lambda req: httpx.Response(403))
    discovered = asyncio.run(retrieval.ainvoke(req))
    result = asyncio.run(retrieval.acquire_full_texts(req, [discovered.source_records[0].source_record_id]))
    assert result.artifacts == []
    assert retrieval.material_store.load(request().scope).raw == []
    assert result.execution_failures
    manifest = retrieval.reference_store.load_manifest("paper-1")
    assert manifest.artifacts == []
    assert manifest.source_records[0].abstract == "A search abstract."
    assert manifest.source_records[0].access_status.value != "full_text_acquired"
    assert not list(tmp_path.rglob("*.txt"))


def test_raw_acquisition_does_not_require_parseable_text(tmp_path):
    empty_article = b'<article class="ltx_document"></article>'
    retrieval, req = acquisition_tool(tmp_path, lambda req: httpx.Response(200, content=empty_article,
                                                headers={"content-type": "text/html"}))
    discovered = asyncio.run(retrieval.ainvoke(req))
    result = asyncio.run(retrieval.acquire_full_texts(req, [discovered.source_records[0].source_record_id]))
    materials = retrieval.material_store.load(request().scope)
    assert len(materials.raw) == 1 and not result.execution_failures
    assert retrieval.material_store.raw(materials.raw[0].ref)[1] == empty_article
    assert retrieval.material_store.load(request().scope).parsed == []


def test_legacy_reader_bridge_remains_available_only_when_explicitly_invoked(tmp_path):
    retrieval, req = acquisition_tool(tmp_path)
    discovered = asyncio.run(retrieval.ainvoke(req))
    result = asyncio.run(retrieval.acquire_full_texts(req, [discovered.source_records[0].source_record_id]))
    raw = retrieval.material_store.load(request().scope).raw[0]
    store = retrieval.reference_store
    with pytest.raises(ValueError, match="must be opened"):
        ReferenceArtifactReaderTool(store).invoke(ReferenceReadRequest(
            subject_paper_id="paper-1", artifact_id=raw.ref.artifact_id))
    # Explicit consumption is outside acquisition and keeps the old API unchanged.
    parsed = LocalMaterialReader(retrieval.material_store).open_sync(open_request(raw)).document
    raw_artifact, parsed_artifact = retrieval.material_store.legacy_artifacts(parsed)
    store.merge_manifest("paper-1", artifacts=[raw_artifact, parsed_artifact])
    read = ReferenceArtifactReaderTool(store).invoke(ReferenceReadRequest(
        subject_paper_id="paper-1", artifact_id=parsed_artifact.artifact_id, max_chars=1000))
    assert "Method uses a graph." in read.text
    checkpoint_read = store.read_document_slice("paper-1", artifact_id=read.artifact_id,
                                                char_start=read.char_start, max_chars=read.char_end-read.char_start)
    assert checkpoint_read == read
    asyncio.run(retrieval.acquire_full_texts(req, [discovered.source_records[0].source_record_id]))
    assert len(retrieval.material_store.load(request().scope).parsed) == 1  # existing derived data is retained
    (tmp_path / "paper-1" / "references" / raw.relative_path).write_bytes(b"tamper")
    with pytest.raises(ValueError):
        store.verify_artifact_file("paper-1", parsed_artifact.artifact_id)


def test_failed_persistence_cannot_return_a_successful_raw_reference(tmp_path, monkeypatch):
    tool = provider(tmp_path, html_response)
    def fail(*args, **kwargs):
        raise OSError("disk full")
    monkeypatch.setattr(tool.store, "save_raw", fail)
    result = tool.acquire_sync(request())
    assert result.status == "failed" and not result.materials
    assert all(not attempt.artifact_refs for attempt in result.attempts)


def test_versions_and_parser_configs_coexist_without_overwriting(tmp_path):
    tool = provider(tmp_path, html_response)
    first = tool.acquire_sync(request()).materials[0]
    target = request().target.model_dump()
    target.update(external_id="2402.03300v2", requested_version="v2")
    second = tool.acquire_sync(request(target=target)).materials[0]
    assert first.ref.artifact_id != second.ref.artifact_id
    assert first.ref.sha256 == second.ref.sha256  # fixture uses identical bytes, different bindings
    reader = LocalMaterialReader(tool.store)
    parsed_first = reader.open_sync(open_request(first)).document
    parsed_second = reader.open_sync(open_request(second)).document
    assert parsed_first.ref.artifact_id != parsed_second.ref.artifact_id
    assert reader.read_sync(read_request(parsed_first)).status == "content"
    assert len(tool.store.load(request().scope).raw) == 2
    assert len(tool.store.load(request().scope).parsed) == 2


def test_agent_projection_does_not_expose_raw_files_as_legacy_reader_handles(tmp_path):
    from novelty_agent_framework.schemas import DatabaseSearchArguments, TaskResearchRequest
    from novelty_agent_framework.tools.database_search import DatabaseSearchTool
    retrieval, req = acquisition_tool(tmp_path)
    tool = DatabaseSearchTool({"arxiv": retrieval}, retrieval.reference_store)
    scope = TaskResearchRequest(**req.model_dump(exclude={"source_id"}))
    discovery = asyncio.run(tool.ainvoke(DatabaseSearchArguments(source_id="arxiv"), scope=scope))
    first = discovery.payload["database_search_result"]["results"][0]
    assert first["abstract_preview"] == "A search abstract."
    assert first["artifact_ids"] == []
    acquired = asyncio.run(tool.ainvoke(DatabaseSearchArguments(source_id="arxiv",
        full_text_source_record_ids=[first["source_record_id"]]), scope=scope))
    from novelty_agent_framework.schemas import ResearchBundle
    ResearchBundle.model_validate(discovery.payload["research_bundle"])
    ResearchBundle.model_validate(acquired.payload["research_bundle"])
    assert retrieval.material_store.load(request().scope).raw
    projected = tool.project_model_context(acquired)["results"][0]
    assert projected["artifact_ids"] == []
    assert projected["full_text_artifact_ids"] == [
        raw.ref.artifact_id for raw in retrieval.material_store.load(request().scope).raw
    ]
    assert not list(tmp_path.rglob("*.txt"))
