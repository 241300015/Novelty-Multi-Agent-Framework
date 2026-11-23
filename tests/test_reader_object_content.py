import asyncio
import base64
from hashlib import sha256
from io import BytesIO

import httpx
from lxml import html
from PIL import Image
import pymupdf
import pytest

from novelty_agent_framework.tools import ReaderTool, ReferenceArtifactReaderTool, ResearcherToolRegistry
from novelty_agent_framework.tools.object_content import _html_grid, _structured, _pdf_table, _render_table
from novelty_agent_framework.core.tool_call_harness import ToolCallHarness
from test_arxiv_materials import acquisition_tool
from test_reader_tool_call_harness_integration import research_scope, ScriptedModelClient, tool_request, ModelResponse
from test_record_reader import invoke


def test_missing_first_asset_keeps_second_and_harness_sends_pixels(tmp_path):
    png = BytesIO()
    Image.new("RGB",(10,10),"green").save(png,"PNG")
    paths = []
    def serve(req):
        path=req.url.path
        paths.append(path)
        if path.endswith("lost.png"):
            return httpx.Response(404)
        if path.endswith("plot.png"):
            return httpx.Response(200,content=png.getvalue(),headers={"content-type":"image/png"})
        return httpx.Response(200,headers={"content-type":"text/html"},content=b'''
          <article class="ltx_document"><p>See Figure 2.</p>
          <figure><img src="2402.03300v1/lost.png"><figcaption>Figure 1: missing</figcaption></figure>
          <figure><img src="2402.03300v1/plot.png"><figcaption>Figure 2: chart</figcaption></figure></article>''')
    retrieval,query=acquisition_tool(tmp_path,serve,full_text_limit=1)
    rec=asyncio.run(retrieval.ainvoke(query)).source_records[0]
    assert any(x.endswith('plot.png') for x in paths)
    reg=ResearcherToolRegistry([ReaderTool(ReferenceArtifactReaderTool(retrieval.reference_store))])
    scope=research_scope().model_copy(update={"subject_paper_id":"paper-1"})
    handle=invoke(reg,scope,operation="open",source_record_id=rec.source_record_id,part="full_text")["handle"]
    obj=next(x for x in invoke(reg,scope,operation="read",handle=handle)["object_refs"] if x["label"]=="Figure 2")
    req=dict(operation="read",handle=handle,object_id=obj["object_id"])
    client=ScriptedModelClient(tool_request({"request":req}),ModelResponse(content="done"))
    result=asyncio.run(ToolCallHarness(client,reg).run(system_prompt="read",initial_user_message="read",scope=scope))
    msg=client.calls[1][0][-1]
    assert msg.role=="user"
    data=base64.b64decode(msg.content[1].image_url.split(",")[1])
    record=next(e.observation.payload["record_result"] for e in result.trace if e.observation)
    assert sha256(data).hexdigest()==record["content"]["sha256"]
    assert Image.open(BytesIO(data)).getpixel((5,5))==(0,128,0)


def test_rich_cell_preserves_svg_pixels(tmp_path):
    root=html.fromstring('<table><tr><th>Run</th><th>Result</th></tr>'
                         '<tr><td>A</td><td><svg width="40" height="40"><circle cx="20" cy="20" r="15" fill="red"/></svg></td></tr></table>')
    rows,native=_html_grid(root)
    assert _structured(rows,native) is None
    from novelty_agent_framework.tools.object_content import RenderUnavailable
    try:
        output=_render_table(root,None,None)
    except RenderUnavailable as exc:
        if "Chromium" in str(exc) or "Playwright" in str(exc):
            pytest.skip(str(exc))
        raise
    colors=Image.open(BytesIO(output)).convert("RGB").getcolors(maxcolors=1_000_000)
    assert sum(count for count,(r,g,b) in colors if r>225 and g<25 and b<25)>200


def test_headerless_pdf_not_promoted_to_table_headers():
    with pymupdf.open() as doc:
        p=doc.new_page()
        for x in (50,125,200):
            p.draw_line((x,50),(x,140))
        for y in (50,80,110,140):
            p.draw_line((50,y),(200,y))
        for i in range(3):
            p.insert_text((55,70+30*i),str(100+i))
            p.insert_text((130,70+30*i),str(120+i))
        assert _pdf_table(p,pymupdf.Rect(49,49,201,141)) is None


def test_unique_hierarchical_headers_and_groups():
    node=html.fromstring('<table><thead><tr><th rowspan="2">Model</th><th colspan="2">English</th></tr>'
                         '<tr><th>A</th><th>B</th></tr></thead><tbody><tr><td colspan="3">Open</td></tr>'
                         '<tr><td>Small</td><td>1</td><td>2</td></tr></tbody></table>')
    rows,native=_html_grid(node)
    table=_structured(rows,native)
    assert table.headers==[["Model"],["English","A"],["English","B"]]
    assert table.rows[0].group=="Open" and table.rows[0].cells==["Small","1","2"]


def test_multiple_grids_rasterize_the_whole_object():
    from novelty_agent_framework.tools.object_content import _object_markup
    root=html.fromstring('<figure><figcaption>Table 2: two panels</figcaption>'
                         '<table><tr><th>Run A</th></tr><tr><td style="background-color:red">0.1</td></tr></table>'
                         '<p>Second panel</p><table><tr><th>Run B</th></tr>'
                         '<tr><td style="background-color:blue">0.9</td></tr></table></figure>')
    assert _structured(*_html_grid(root)) is None
    markup=_object_markup(root,None,None)
    assert all(x in markup for x in ["Run A","0.1","Second panel","Run B","0.9"])
    from novelty_agent_framework.tools.object_content import RenderUnavailable
    try:
        img=Image.open(BytesIO(_render_table(root,None,None))).convert("RGB")
    except RenderUnavailable as exc:
        if "Chromium" in str(exc) or "Playwright" in str(exc):
            pytest.skip(str(exc))
        raise
    colors={rgb for count,rgb in img.getcolors(maxcolors=1_000_000) if count>10}
    assert (255,0,0) in colors and (0,0,255) in colors
