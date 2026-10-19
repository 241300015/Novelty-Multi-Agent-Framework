"""Small, dependency-free helpers for provider-supplied JATS-like XML."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from html.parser import HTMLParser


@dataclass(frozen=True)
class ParsedXmlFullText:
    title: str
    text: str
    sections: dict[str, str]


class _PlainTextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def markup_to_text(content: object) -> str:
    """Normalize an API field that may contain a small HTML/XML fragment."""

    value = str(content or "")
    if "<" not in value:
        return _normalize_text(value)
    parser = _PlainTextParser()
    try:
        parser.feed(value)
    except Exception:
        return _normalize_text(re.sub(r"<[^>]+>", " ", value))
    return _normalize_text(" ".join(parser.parts))


def parse_xml_full_text(content: str, *, fallback_title: str) -> ParsedXmlFullText | None:
    """Extract readable text and section hints from JATS or simplified article XML."""

    try:
        root = ET.fromstring(content)
    except ET.ParseError:
        return None

    record = _first_element(root, {"article", "book-part-wrapper", "book-part"})
    if record is None:
        record = root
    title_element = _first_element(
        record,
        {"article-title", "chapter-title", "book-title", "document-title"},
    )
    if title_element is None:
        title_element = _first_direct_child(record, "title")
    if title_element is None:
        front = _first_element(record, {"front", "document-meta"})
        title_element = _first_element(front, {"title"}) if front is not None else None
    title = _element_text(title_element) or fallback_title
    abstract_element = _first_element(record, {"abstract"})
    body_element = _first_element(record, {"body"})
    abstract = _element_text(abstract_element)
    body = _element_text(body_element)
    if not abstract and not body:
        return None

    sections: dict[str, str] = {}
    if abstract:
        sections["Abstract"] = abstract
    if body_element is not None:
        for section in _elements(body_element, "sec"):
            section_title = _element_text(_first_direct_child(section, "title"))
            section_text = _element_text_without_direct_title(section)
            if section_text:
                _store_section(sections, section_title or "Section", section_text)
        if body and len(sections) == (1 if abstract else 0):
            sections["Body"] = body

    parts: list[str] = []
    if abstract:
        parts.extend(("Abstract", abstract))
    if body:
        parts.extend(("Body", body))
    return ParsedXmlFullText(title=title, text="\n\n".join(parts), sections=sections)


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def _first_element(root: ET.Element, names: set[str]) -> ET.Element | None:
    if _local_name(root.tag) in names:
        return root
    return next((item for item in root.iter() if _local_name(item.tag) in names), None)


def _elements(root: ET.Element, name: str):
    return (item for item in root.iter() if _local_name(item.tag) == name)


def _first_direct_child(root: ET.Element, name: str) -> ET.Element | None:
    return next((item for item in root if _local_name(item.tag) == name), None)


def _element_text(element: ET.Element | None) -> str:
    if element is None:
        return ""
    return _normalize_text(" ".join(element.itertext()))


def _element_text_without_direct_title(element: ET.Element) -> str:
    parts: list[str] = []
    if element.text:
        parts.append(element.text)
    for child in element:
        if _local_name(child.tag) == "title":
            if child.tail:
                parts.append(child.tail)
        else:
            parts.extend(child.itertext())
    return _normalize_text(" ".join(parts))


def _normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _store_section(sections: dict[str, str], title: str, text: str) -> None:
    key = title
    suffix = 2
    while key in sections:
        key = f"{title} ({suffix})"
        suffix += 1
    sections[key] = text
