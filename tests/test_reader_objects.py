"""Object addresses, locality, integrity and bounded reuse of layout inference."""
from types import SimpleNamespace

import pytest
import pymupdf
from pydantic import ValidationError
from novelty_agent_framework.schemas.reader import PdfObjectLocation
from novelty_agent_framework.tools import reader_objects as objects
from test_full_text_record_reader import corpus, open_format


@pytest.mark.parametrize("rect", [[0,0,0,2], [-1,0,2,2], [0,0,float("inf"),2]])
def test_invalid_pdf_box(rect):
    with pytest.raises(ValidationError):
        PdfObjectLocation(page=1, bbox=rect)


def test_figure_component_coverage():
    layout = pymupdf.Rect(76,92,507,243)
    image = pymupdf.Rect(461,133,521,197)
    vector = pymupdf.Rect(93,86,491,249)
    background = pymupdf.Rect(0,0,595,842)
    result = objects._include_components(layout, [image, vector, background], background)
    assert result.contains(image) and result.contains(vector) and not result.contains(background)


def test_nested_objects_and_anonymous_data_table():
    source = b"""<article class="ltx_document"><section><figure id="t" class="ltx_table">
    <figcaption>Table 2: Results</figcaption><table><tr><th>A</th></tr></table></figure>
    <table><tr><th>Name</th><td>Item</td></tr></table></section></article>"""
    rows = list(objects._html_objects(source,"utf-8"))
    assert [(k,l) for k,l,_ in rows] == [("table","Table 2"),("table",None)]
    refs = [SimpleNamespace(label=label, locations=[loc]) for _,label,loc in rows]
    def discover(text,start,end,xpath):
        origin=SimpleNamespace(kind="html_xpath",value=xpath)
        entry=SimpleNamespace(parsed_char_start=start,parsed_char_end=end,locator=origin)
        return objects.discover(refs,text,[entry],start,end)
    assert not discover("\n",0,1,"/html/body/article")
    assert discover("See Table 2.",6,10,"/html/body/article/section")[:1] == refs[:1]


def test_repeated_open_reuses_verified_index(corpus, monkeypatch):
    call, record, materials = corpus
    calls = []
    original = objects.build_index
    def counted(*args):
        calls.append(1)
        return original(*args)
    monkeypatch.setattr(objects, "build_index", counted)
    h=open_format(call, record, "pdf")["handle"]
    assert open_format(call, record, "pdf")["handle"] == h and len(calls)==1
    assert call(operation="read",handle=h,object_id="0"*64)["status"]=="invalid_address"
    from novelty_agent_framework.schemas.reader import IndexedFullTextHandle
    parsed=IndexedFullTextHandle.model_validate(h)
    file=materials._path(parsed, f"documents/{parsed.work_id}/objects/{parsed.object_index_sha256}.json")
    file.write_bytes(b"altered")
    assert open_format(call,record,"pdf")["status"]=="integrity_error"
    assert call(operation="read",handle=h,object_id="0"*64)["status"]=="integrity_error"


def test_discover_then_locate_and_reject_other_snapshot(tmp_path):
    import asyncio
    import httpx
    from test_arxiv_materials import acquisition_tool
    from test_reader_tool_call_harness_integration import research_scope
    from test_record_reader import invoke
    from novelty_agent_framework.tools import ReaderTool, ReferenceArtifactReaderTool, ResearcherToolRegistry
    from novelty_agent_framework.schemas.reader import ReaderRead

    body = b'''<article class="ltx_document"><p>Compare Table 2 with Figure 2.</p>
        <figure id="F2"><img src="local.png"><figcaption>Figure 2: Chart</figcaption></figure>
        <figure id="T2" class="ltx_table"><figcaption>Table 2: Values</figcaption></figure></article>'''
    retrieval, query = acquisition_tool(tmp_path, lambda req: httpx.Response(200, content=body,
                                      headers={"Content-Type":"text/html; charset=utf-8"}),
                                      versions=("v1","v2"), full_text_limit=2)
    records = asyncio.run(retrieval.ainvoke(query)).source_records
    scope = research_scope().model_copy(update={"subject_paper_id":"paper-1"})
    def call(**args):
        reg = ResearcherToolRegistry([ReaderTool(ReferenceArtifactReaderTool(retrieval.reference_store))])
        return invoke(reg, scope, **args)
    h = open_format(call, records[0],"html")["handle"]
    other = open_format(call, records[1],"html")["handle"]
    out = call(operation="read",handle=h,max_tokens=32)
    refs = {o["label"]:o for o in out["object_refs"]}
    for label, eid in (("Table 2","T2"),("Figure 2","F2")):
        ref = refs[label]
        assert ref["locations"][0]["element_id"] == eid
        assert call(operation="read", handle=h, object_id=ref["object_id"])["object"] == ref
        assert call(operation="read", handle=other, object_id=ref["object_id"])["status"] == "invalid_address"
        with pytest.raises(ValidationError):
            ReaderRead(operation="read",handle=h,object_id=ref["object_id"],section_id="0")


def test_object_overflow_replay_and_cursor_scope(tmp_path):
    import asyncio
    import httpx
    from test_arxiv_materials import acquisition_tool
    from test_reader_tool_call_harness_integration import research_scope
    from test_record_reader import invoke
    from novelty_agent_framework.tools import ReaderTool, ReferenceArtifactReaderTool, ResearcherToolRegistry

    source = b'<article class="ltx_document"><section>' + b'<table><tr><th>x</th></tr></table>' * 33 + b'</section></article>'
    retrieval, query = acquisition_tool(tmp_path, lambda req: httpx.Response(200, content=source,
                headers={"Content-Type":"text/html; charset=utf-8"}), full_text_limit=1)
    record = asyncio.run(retrieval.ainvoke(query)).source_records[0]
    scope = research_scope().model_copy(update={"subject_paper_id":"paper-1"})
    def call(**args):
        reg = ResearcherToolRegistry([ReaderTool(ReferenceArtifactReaderTool(retrieval.reference_store))])
        return invoke(reg, scope, **args)
    h=open_format(call,record,"html")["handle"]
    args=dict(operation="read",handle=h)
    page=call(**args)
    refs=list(page["object_refs"])
    assert len(refs)==16
    cursor=page["next_object_cursor"]
    assert call(**args,object_cursor=dict(cursor,slice_digest="0"*64))["status"]=="invalid_address"
    while page["next_object_cursor"]:
        page=call(**args,object_cursor=page["next_object_cursor"])
        assert page["status"]=="object_refs" and len(page["object_refs"])<=16
        refs.extend(page["object_refs"])
    assert len({o["object_id"] for o in refs})==33
    assert call(**args,object_id=refs[-1]["object_id"])["object"]==refs[-1]
