"""基于有序锚点表的链式章节切分。"""

from __future__ import annotations

import re

from .clean import clean_section

SECTION_ORDER = (
    "abstract",
    "keywords",
    "english_abstract",
    "introduction",
    "methods",
    "results",
    "discussion",
    "conclusion",
    "reference",
)

# 每节一组起始锚点（行首 + 可选编号/标题标记），按顺序取第一个命中
SECTION_PATTERNS: dict[str, tuple[str, ...]] = {
    "abstract": (
        r"(?m)^\s*#*\s*摘\s*要\s*$",
        r"(?m)^\s*#*\s*摘要\s*$",
        r"(?m)^\s*Abstract\s*$",
    ),
    "keywords": (
        r"(?m)^\s*(?:关键词|关键字|Key\s*words?)\s*[:：]",
    ),
    "english_abstract": (
        r"(?m)^\s*#*\s*英文摘要\s*$",
        r"(?m)^\s*#*\s*Abstract\s*$",
    ),
    "introduction": (
        r"(?m)^\s*#*\s*(?:第[一二三四五六七八九十\d]+章\s*)?(?:引言|绪论|Introduction)\s*$",
    ),
    "methods": (
        r"(?m)^\s*#*\s*(?:\d+(?:\.\d+)*\s*)?(?:研究方法|实验方法|方法|Methods?|Methodology)\s*$",
    ),
    "results": (
        r"(?m)^\s*#*\s*(?:\d+(?:\.\d+)*\s*)?(?:实验结果|结果|Results?)\s*$",
    ),
    "discussion": (
        r"(?m)^\s*#*\s*(?:\d+(?:\.\d+)*\s*)?(?:讨论|分析|Discussion)\s*$",
    ),
    "conclusion": (
        r"(?m)^\s*#*\s*第[一二三四五六七八九十\d]+章\s*(?:结论|总结|总结与展望|展望)\s*$",
        r"(?m)^\s*#*\s*(?:\d+(?:\.\d+)*\s*)?(?:结论|总结|总结与展望|展望|Conclusion)\s*$",
    ),
    "reference": (
        r"(?m)^\s*#*\s*(?:参考文献|References?|文献)\s*$",
    ),
}

MAX_SECTION_CHARS = 20_000

_REF_ITEM = re.compile(r"[\[【]\d+[\]】]")
_KEYWORDS_EN_RE = re.compile(r"(?im)^\s*key\s*words?\s*[:：]\s*(.+?)\s*$")
_TOC_PATTERNS = (
    r"(?m)^\s*目\s*录\s*$",
    r"(?m)^\s*Table\s+of\s+Contents\s*$",
    r"(?m)^\s*CONTENTS\s*$",
)


def split_sections(text: str) -> tuple[dict[str, str], list[str]]:
    """链式切分：每节内容 = 本节锚点之后到下一节锚点之前。"""

    anchors: dict[str, tuple[int, int]] = {}
    for section in SECTION_ORDER:
        if section == "english_abstract":
            # 英文摘要必须位于中文关键词（或摘要）之后，避免抢占纯英文论文的 abstract
            boundary = None
            if "keywords" in anchors:
                boundary = anchors["keywords"][1]
            elif "abstract" in anchors:
                boundary = anchors["abstract"][1]
            match = _find_anchor(text, section, after=boundary or 0)
        else:
            match = _find_anchor(text, section)
        if match is not None:
            anchors[section] = match

    ordered = [section for section in SECTION_ORDER if section in anchors]
    sections: dict[str, str] = {}
    warnings: list[str] = []

    for index, section in enumerate(ordered):
        _, content_start = anchors[section]
        next_start = anchors[ordered[index + 1]][0] if index + 1 < len(ordered) else None
        end = _resolve_end(text, content_start, next_start, section, warnings)
        if section == "keywords":
            page_bound = _next_page_marker(text, content_start)
            if page_bound is not None:
                end = min(end, page_bound)
        if section == "reference":
            end = len(text)  # 参考文献始终延伸到文末
        if section == "english_abstract":
            toc_bound = _find_toc_boundary(text, content_start, end)
            if toc_bound is not None:
                end = toc_bound  # 英文摘要提前结束于目录标题，避免吞入目录
        cleaned = clean_section(text[content_start:end], section)
        if cleaned:
            sections[section] = cleaned
        else:
            warnings.append(f"{section}: 切分结果为空")

    for section in SECTION_ORDER:
        if section not in sections:
            warnings.append(f"{section}: 未检测到")

    if "abstract" not in sections:
        fallback = _first_page_fallback(text)
        if fallback:
            sections["abstract"] = fallback
            warnings.append("abstract: 使用首页回退")
    return sections, warnings


def _find_anchor(
    text: str,
    section: str,
    *,
    after: int = 0,
) -> tuple[int, int] | None:
    for pattern in SECTION_PATTERNS[section]:
        match = re.search(pattern, text[after:], re.MULTILINE | re.IGNORECASE)
        if match:
            return after + match.start(), after + match.end()
    return None


def _find_toc_boundary(text: str, start: int, end: int) -> int | None:
    """在 [start, end) 内查找目录标题位置，作为英文摘要的提前结束边界。"""

    chunk = text[start:end]
    for pattern in _TOC_PATTERNS:
        match = re.search(pattern, chunk, re.MULTILINE | re.IGNORECASE)
        if match:
            return start + match.start()
    return None


def _resolve_end(
    text: str,
    content_start: int,
    next_start: int | None,
    section: str,
    warnings: list[str],
) -> int:
    if next_start is not None:
        return next_start
    tail = text[content_start:]
    page_match = re.search(r"(?m)^\s*#\s*Page\s*\d+", tail)
    if page_match and page_match.start() < MAX_SECTION_CHARS:
        return content_start + page_match.start()
    end = min(len(text), content_start + MAX_SECTION_CHARS)
    warnings.append(f"{section}: 结束锚点缺失，截断至 {end - content_start} 字符")
    return end


def _next_page_marker(text: str, pos: int) -> int | None:
    match = re.search(r"(?m)^\s*#\s*Page\s*\d+", text[pos:])
    return pos + match.start() if match else None


def _first_page_fallback(text: str) -> str:
    page_two = re.search(r"(?m)^#\s*Page\s*2\b", text)
    chunk = text[: page_two.start()] if page_two else text[:2000]
    return clean_section(chunk, "abstract")[:1200]


def listify_references(ref_text: str) -> list[str]:
    """把参考文献文本拆成条目列表（[n] 开头为新条目，其余为续行）。"""

    if not ref_text.strip():
        return []
    text = _REF_ITEM.sub(lambda match: "\n" + match.group(0), ref_text)
    items: list[str] = []
    current: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if _REF_ITEM.match(line):
            if current:
                items.append(" ".join(current))
            current = [line]
        else:
            current.append(line)
    if current:
        items.append(" ".join(current))
    return items


def parse_keywords_zh(text: str) -> list[str]:
    """把中文关键词段文本按分隔符拆成关键词列表。"""

    items = [part.strip() for part in re.split(r"[；;，,]", text) if part.strip()]
    return _dedupe_keywords(items)


def extract_keywords_en(text: str) -> list[str]:
    """从英文摘要文本中提取 ``keywords:`` 行并解析为关键词列表。"""

    if not text:
        return []
    match = _KEYWORDS_EN_RE.search(text)
    if match is None:
        return []
    items = [
        part.strip()
        for part in re.split(r"[,，;；]", match.group(1))
        if part.strip()
    ]
    return _dedupe_keywords(items)


def _dedupe_keywords(items: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            result.append(item)
    return result
