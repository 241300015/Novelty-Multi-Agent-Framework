"""Read one verified figure/table. No external URL is fetched during a read."""
from base64 import b64decode
from io import BytesIO
import re

from PIL import Image
import pymupdf

from ..schemas.reader import LocatedObject, ObjectImage, ObjectTable, ReaderIssue, TableRow
from .material_reader import _html_article


def _image(store, handle, data):
    with Image.open(BytesIO(data)) as im:
        if im.width * im.height > 24_000_000:
            raise ValueError("image exceeds pixel limit")
        im.thumbnail((1800, 1800))
        image = im.convert("RGBA" if "A" in im.getbands() or "transparency" in im.info else "RGB")
        stream = BytesIO()
        image.save(stream, "PNG")
        body = stream.getvalue()
        if len(body) > 8 * 1024 * 1024:
            raise ValueError("image exceeds byte limit")
        return ObjectImage(sha256=store.save_object_png(handle, body), byte_size=len(body),
                           width=image.width, height=image.height)


def _span(node, name):
    value = node.get(name)
    if value is None:
        prefix = f"ltx_{name}_"
        value = next((c[len(prefix):] for c in node.classes if c.startswith(prefix)), "1")
    return int(value) if re.fullmatch(r"[1-9]\d?", value) else 0


def _grids(node):
    # Nested grids inside a cell belong to that cell. Sibling grids are
    # separate parts of the same located object and must all be delivered.
    candidates=[el for el in node.iter() if isinstance(el.tag,str)
                and (el.tag=="table" or "ltx_tabular" in el.classes)]
    members=set(candidates)
    return [el for el in candidates if not any(parent in members for parent in el.iterancestors())]


def _html_grid(node):
    grids = _grids(node)
    grid = grids[0] if len(grids)==1 else None
    if grid is None:
        return [], False
    native = grid.tag == "table"
    rows = grid.xpath("./tr|./thead/tr|./tbody/tr|./tfoot/tr") if native else grid.xpath("./*")
    result = []
    for row in rows:
        if not native and "ltx_tr" not in row.classes:
            continue
        cells = [(c, " ".join(c.text_content().split()), _span(c,"colspan"), _span(c,"rowspan"))
                 for c in row if isinstance(c.tag, str) and (c.tag in {"td","th"} or "ltx_td" in c.classes)]
        if cells:
            result.append(cells)
            if len(result)>250:
                break
    return result, native


def _structured(rows, native):
    if not 2 <= len(rows) <= 250:
        return None
    if any(c.xpath(".//table") for row in rows for c,_,_,_ in row):
        return None
    if any(c.xpath(".//svg|.//img|.//sup|.//sub|.//math|.//canvas|.//input|.//ruby")
           for row in rows for c,_,_,_ in row):
        return None
    width = sum(c[2] for c in rows[0])
    if not 2 <= width <= 64 or any(not cs or not rs for row in rows for _,_,cs,rs in row):
        return None
    if native:
        depth = 0
        for row in rows:
            if not all(c.tag=="th" or c.xpath("ancestor::thead") for c,_,_,_ in row):
                break
            depth += 1
    else:
        # LaTeXML marks grouped column headers with spans; plain td-only rows
        # without such evidence cannot establish a header.
        depth = 2 if any(c[2]>1 for c in rows[0]) and any(c[3]>1 for c in rows[0]) else 0
    if not 1 <= depth < len(rows) or depth > 8:
        return None
    matrix = [[None]*width for _ in range(depth)]
    for r,row in enumerate(rows[:depth]):
        col = 0
        for _,value,cs,rs in row:
            while col < width and matrix[r][col] is not None:
                col += 1
            if not value or col+cs > width:
                return None
            for rr in range(r,min(depth,r+rs)):
                if any(x is not None for x in matrix[rr][col:col+cs]):
                    return None
                matrix[rr][col:col+cs] = [value]*cs
            col += cs
    if any(v is None for row in matrix for v in row):
        return None
    headers = [list(dict.fromkeys(path)) for path in zip(*matrix)]
    if len({tuple(path) for path in headers}) != width:
        return None
    group = None
    values = []
    for row in rows[depth:]:
        if len(row)==1 and row[0][2:]==(width,1):
            group = row[0][1]
        elif len(row)==width and all(cs==rs==1 for _,_,cs,rs in row):
            values.append(TableRow(group=group,cells=[v for _,v,_,_ in row]))
        else:
            return None
    return ObjectTable(headers=headers,rows=values) if values else None


class RenderUnavailable(Exception):
    pass


def _object_markup(node, store, raw):
    """Serialize supported visual markup with no active content or external URIs."""
    from copy import deepcopy
    from base64 import b64encode
    from lxml import etree
    cell = deepcopy(node)
    for el in list(cell.iter()):
        if not isinstance(el.tag, str):
            continue
        if el.tag in {"script","style","iframe","object","use","foreignobject","image","link"}:
            raise RenderUnavailable(f"Unsupported embedded element: {el.tag}")
        if el.tag == "img":
            src = el.get("src", "")
            if src.startswith("data:image/png;base64,") and len(src)<=12_000_000:
                image = b64decode(src.split(",",1)[1],validate=True)
            else:
                image = store.html_asset(raw,src)
            if not image:
                raise RenderUnavailable("Embedded cell image is unavailable.")
            el.attrib.clear()
            el.set("src","data:image/png;base64,"+b64encode(image).decode())
            continue
        attrs = dict(el.attrib)
        classes = set(attrs.get("class","").split())
        if "ltx_tabular" in classes:
            el.tag = "table"
        elif "ltx_tr" in classes:
            el.tag = "tr"
        elif "ltx_td" in classes:
            el.tag = "td"
        if el.tag in {"td","th"}:
            for name in ("colspan","rowspan"):
                span=_span(el,name)
                if not span:
                    raise RenderUnavailable("Invalid HTML cell span.")
                attrs[name]=str(span)
        el.attrib.clear()
        for key,val in attrs.items():
            if key.lower() in {"d","x","y","x1","y1","x2","y2","cx","cy","r","rx","ry",
                               "width","height","points","viewbox","fill","stroke","stroke-width",
                               "transform","colspan","rowspan","class","mathvariant","display"}:
                if "url" not in val.lower():
                    el.set(key,val)
            elif key=="style":
                css = []
                for entry in val.split(";"):
                    prop,_,value = entry.partition(":")
                    if (prop.strip().lower() in {"color","background-color","font-weight","font-style",
                         "text-align","vertical-align","font-size","fill","stroke","width","height"}
                            and re.fullmatch(r"[a-zA-Z0-9#.,()% +\-]+",value) and "url" not in value.lower()):
                        css.append(prop+":"+value)
                if css:
                    el.set("style",";".join(css))
    return etree.tostring(cell,method="html",encoding="unicode",with_tail=False)


def _render_table(node, store, raw):
    try:
        from playwright.sync_api import Error, sync_playwright
    except ImportError as exc:
        raise RenderUnavailable("Playwright is unavailable.") from exc
    markup = _object_markup(node,store,raw)
    if len(markup)>12_000_000:
        raise RenderUnavailable("HTML grid exceeds rendering limit.")
    policy = """<meta http-equiv="Content-Security-Policy" content="default-src 'none'; img-src data:; style-src 'unsafe-inline'">"""
    try:
        with sync_playwright() as engine:
            browser=engine.chromium.launch()
            try:
                page=browser.new_page(java_script_enabled=False,offline=True)
                page.set_default_timeout(5000)
                page.set_content(policy+'<style>table{border-collapse:collapse}td{border:1px solid;padding:6px}#reader-object{display:inline-block}figure{margin:0}</style><div id="reader-object">'+markup+'</div>')
                table=page.locator("#reader-object")
                box=table.bounding_box()
                if not box or box["width"]*box["height"]>12_000_000:
                    raise RenderUnavailable("HTML grid exceeds pixel limit.")
                return table.screenshot()
            finally:
                browser.close()
    except Error as exc:
        raise RenderUnavailable("Offline Chromium rendering unavailable.") from exc


def _pdf_table(page, rect):
    # A complete ruled grid with one explicit header row. Multi-level,
    # headerless and merged grids are delivered visually.
    tables = page.find_tables(clip=rect, strategy="lines").tables
    if len(tables)!=1:
        return None
    t=tables[0]
    grid=t.extract()
    names=t.header.names
    if (t.header.external or not names or len(names)<2 or len(grid)<2 or grid[0]!=names
            or any(not n or not n.strip() for n in names) or len(set(names))!=len(names)
            or (pymupdf.Rect(t.bbox)&rect).get_area() < 0.90*rect.get_area()
            or any(len(row)!=len(names) or None in row for row in grid)):
        return None
    # PyMuPDF may call the first row of any grid its header. Require
    # independently visible header typography, distinct from the data rows.
    if not all(any(ch.isalpha() for ch in name) for name in names):
        return None
    spans = [s for block in page.get_text("dict",clip=rect,flags=pymupdf.TEXTFLAGS_TEXT)["blocks"]
             for line in block.get("lines",[]) for s in line["spans"] if s["text"].strip()]
    header_box=pymupdf.Rect(t.header.bbox)
    head=[s for s in spans if header_box.contains(pymupdf.Rect(s["bbox"]).tl + (1,1))]
    data=[s for s in spans if s not in head]
    if not head or not all(s["flags"] & 16 for s in head) or not any(not (s["flags"] & 16) for s in data):
        return None
    if any(not (pymupdf.Rect(im["bbox"]) & rect).is_empty for im in page.get_image_info()):
        return None
    return ObjectTable(headers=[[n] for n in names], rows=[TableRow(cells=row) for row in grid[1:]])


def read_object(store, raw, handle, obj, budget):
    _, body=store.raw(raw.ref)
    loc,=obj.locations
    image=table=None
    warnings=[]
    if handle.source_format=="pdf":
        with pymupdf.open(stream=body,filetype="pdf") as doc:
            page=doc[loc.page-1]
            page.set_rotation(0)
            rect=pymupdf.Rect(loc.bbox) & page.rect
            if rect.is_empty:
                return ReaderIssue(status="invalid_address",message="Empty PDF object region.")
            if obj.kind=="table":
                table=_pdf_table(page,rect)
                if table is None:
                    warnings.append("PDF cell/header alignment could not be verified; returning the original region as pixels.")
            if table is None:
                scale=min(2.5,1800/max(rect.width,rect.height))
                image=page.get_pixmap(matrix=pymupdf.Matrix(scale,scale),clip=rect,alpha=False).tobytes("png")
    else:
        article=_html_article(body,raw.format.charset)
        nodes=article.getroottree().xpath(loc.xpath)
        if len(nodes)!=1 or (loc.element_id and nodes[0].get("id")!=loc.element_id):
            return ReaderIssue(status="invalid_address",message="HTML object no longer resolves.")
        node=nodes[0]
        images=node.xpath(".//img/@src")
        if obj.kind=="table":
            rows,native=_html_grid(node)
            table=_structured(rows,native)
            if table is None:
                warnings.append("HTML header/cell alignment could not be verified; rendering cell layout.")
                if _grids(node):
                    try:
                        image=_render_table(node,store,raw)
                    except RenderUnavailable as exc:
                        return LocatedObject(object=obj,warnings=[*warnings,str(exc)])
        if table is None and image is None and len(images)==1:
            src=images[0]
            if src.startswith("data:image/png;base64,") and len(src)<=12_000_000:
                image=b64decode(src.split(",",1)[1],validate=True)
            else:
                image=store.html_asset(raw,src)
    if table is not None:
        if len(table.model_dump_json().encode())>budget:
            return ReaderIssue(status="budget_too_small",message="Budget cannot fit the complete structured table.")
        return LocatedObject(object=obj, content=table, warnings=warnings)
    if image is None:
        warnings.append("Object pixels unavailable in the acquired material.")
        return LocatedObject(object=obj,warnings=warnings)
    return LocatedObject(object=obj,content=_image(store,handle,image),warnings=warnings)
