"""Object identity and discovery for immutable Reader snapshots."""
from collections import OrderedDict
from hashlib import sha256
import re
from threading import Lock

import pymupdf

from ..schemas.materials import canonical_material_json
from ..schemas.reader import ObjectRefsCursor, ObjectRefsPage, IndexedFullTextHandle, HtmlObjectLocation, PdfObjectLocation, ReaderObject, ReaderObjectIndex
from .material_reader import _html_article

# Captions and prose use the same normalization. Keep exact labels distinct (2 vs 20).
_LABEL = re.compile(r"\b(fig(?:ure)?\.?|table)\s+([A-Z]?\d+(?:\.\d+)*[a-z]?|[IVXLCDM]+)\b", re.I)
_LAYOUT_LOCK = Lock()


def _label(match):
    return ("Table" if match[1].lower() == "table" else "Figure") + " " + match[2]


def _html_objects(body, charset):
    article = _html_article(body, charset)
    dom = article.getroottree()
    tables = ".//table[(caption or .//th or @role='table') and not(ancestor::figure or ancestor::table)]"
    for node in article.xpath(".//figure | " + tables):
        captions = node.xpath("./figcaption | ./caption")
        caption = " ".join(captions[0].text_content().split()) if captions else ""
        match = _LABEL.match(caption)
        label = _label(match) if match else None
        is_table = (node.tag == "table" or "ltx_table" in node.classes or (label and label.startswith("Table ")))
        yield ("table" if is_table else "figure", label,
               HtmlObjectLocation(xpath=dom.getpath(node), element_id=node.get("id") or None))


def _pdf_page(page):
    # Layout inference produces picture/table regions directly, including vector artwork.
    page.set_rotation(0)
    page.get_layout()
    boxes = page.layout_information or []
    captions = []
    objects = []
    for *coords, category in boxes:
        rect = pymupdf.Rect(coords) & page.rect
        if rect.is_empty:
            continue
        if category in {"picture", "table"}:
            objects.append(("figure" if category == "picture" else "table", rect))
        elif category in {"caption", "text"}:
            words = " ".join(page.get_textbox(rect + (-1, -1, 1, 1)).split())
            match = _LABEL.match(words)
            if match and (category == "caption" or re.match(r"\s*[:.|–—]", words[match.end():])):
                captions.append((_label(match), rect))
    # Associate a nearby, horizontally overlapping caption with its nearest same-kind
    # box. Leave ambiguous/unlabelled objects addressable by location alone.
    labels = {}
    for label, cap in captions:
        eligible = []
        for i, (kind, rect) in enumerate(objects):
            if kind != label.split()[0].lower():
                continue
            overlap = min(rect.x1, cap.x1) - max(rect.x0, cap.x0)
            gap = max(cap.y0 - rect.y1, rect.y0 - cap.y1, 0)
            if overlap >= min(rect.width, cap.width) * .5 and gap <= 48:
                eligible.append((gap, i))
        eligible.sort()
        if eligible and (len(eligible) == 1 or eligible[1][0] - eligible[0][0] > 2):
            labels.setdefault(eligible[0][1], []).append(label)
    images = [pymupdf.Rect(i["bbox"]) for i in page.get_image_info()] if objects else []
    vectors = page.cluster_drawings() if objects else []
    # Include thin horizontal table rules as well as the layout model's text box.
    rules = [d["rect"] for d in page.get_drawings() if d["rect"].height <= 1] if objects else []
    for i, (kind, rect) in enumerate(objects):
        rect = _include_components(rect, images + vectors if kind == "figure" else rules, page.rect)
        names = labels.get(i, [])
        yield kind, names[0] if len(names) == 1 else None, PdfObjectLocation(page=page.number+1, bbox=tuple(rect))


def _include_components(box, components, page_rect):
    """Keep mostly enclosed image/vector components whole, without absorbing page backgrounds."""
    padded = box + (-6, -6, 6, 6)
    envelope = box + (-18, -18, 18, 18)
    result = pymupdf.Rect(box)
    for component in components:
        r = pymupdf.Rect(component)
        # A PDF rule can have zero height and would vanish in rectangle intersection.
        if r.height <= 1:
            r = r + (0, -.5, 0, .5)
        if r.get_area() and envelope.contains(r) and (r & padded).get_area() >= .65 * r.get_area():
            result |= r
    return result & page_rect


def build_index(handle, body, charset):
    warnings = []
    if handle.source_format == "html":
        rows = list(_html_objects(body, charset))
    else:
        # Import activates the official layout backend. Serialize model calls.
        import pymupdf.layout
        with _LAYOUT_LOCK, pymupdf.open(stream=body, filetype="pdf") as doc:
            rows = []
            for page in doc:
                rows.extend(_pdf_page(page))
    objects = []
    for kind, label, loc in rows:
        address = [handle.model_dump(mode="json"), kind, loc.model_dump(mode="json")]
        object_id = sha256(canonical_material_json(address)).hexdigest()
        objects.append(ReaderObject(object_id=object_id, kind=kind, label=label, locations=[loc]))
    return ReaderObjectIndex(source=handle, objects=objects, warnings=warnings)


def discover(objects, text, entries, a, b):
    # A page boundary may bisect a label. Match only mentions overlapping returned text.
    window = max(0, a - 32)
    mentions = {_label(m) for m in _LABEL.finditer(text[window:b + 32])
                if window + m.start() < b and window + m.end() > a}
    origins = [entry.locator for entry in entries
               if entry.parsed_char_start < b and entry.parsed_char_end > a and
               text[max(a, entry.parsed_char_start):min(b, entry.parsed_char_end)].strip()]
    def local(obj):
        for loc in obj.locations:
            for origin in origins:
                if (obj.label is None and isinstance(loc, PdfObjectLocation)
                        and origin.kind == "pdf_page" and origin.page == loc.page):
                    return True
                if isinstance(loc, HtmlObjectLocation) and origin.kind == "html_xpath":
                    if (loc.xpath == origin.value or
                            (obj.label is None and not origin.value.endswith("/article") and
                             loc.xpath.startswith(origin.value + "/"))):
                        return True
        return False
    # Explicit textual mentions take precedence over nearby objects in the same region.
    direct = [o for o in objects if o.label in mentions]
    nearby = [o for o in objects if o.label not in mentions and local(o)]
    return direct + nearby


# Cache only verified sidecar checksums. Each shard bounds both memory and
# concurrent misses for the same snapshot, including across Reader instances.
_INDEX_SHARDS = [(Lock(), OrderedDict()) for _ in range(8)]


class DetectionUnavailable(Exception):
    pass


def indexed_handle(store, raw, handle):
    key = (str(store.references.output_root.resolve()), raw.format.charset,
           canonical_material_json(handle.model_dump(mode="json")))
    lock, recent = _INDEX_SHARDS[hash(key) % len(_INDEX_SHARDS)]
    with lock:
        checksum = recent.get(key)
        if checksum is not None:
            pinned = IndexedFullTextHandle(**handle.model_dump(), object_index_sha256=checksum)
            store.reader_objects(pinned, handle)
            recent.move_to_end(key)
            return pinned
        _, body = store.raw(raw.ref)
        try:
            index = build_index(handle, body, raw.format.charset)
        except (ImportError, ValueError, RuntimeError) as exc:
            raise DetectionUnavailable(type(exc).__name__) from exc
        checksum = store.save_reader_objects(index)
        recent[key] = checksum
        if len(recent) > 8:
            recent.popitem(last=False)
        return IndexedFullTextHandle(**handle.model_dump(), object_index_sha256=checksum)


def reference_page(index, handle, text, entries, start, end, cursor=None):
    key = sha256(canonical_material_json([handle.model_dump(mode="json"), start, end])).hexdigest()
    if cursor and (cursor.slice_digest != key or not 0 <= start < end <= len(text) or end-start > 16000):
        return None
    refs = discover(index.objects, text, entries, start, end)
    offset = cursor.offset if cursor else 0
    if cursor and offset >= len(refs):
        return None
    following = offset + 16
    more = (ObjectRefsCursor(slice_digest=key, start=start, end=end, offset=following)
            if following < len(refs) else None)
    return ObjectRefsPage(object_refs=refs[offset:following], next_object_cursor=more)
