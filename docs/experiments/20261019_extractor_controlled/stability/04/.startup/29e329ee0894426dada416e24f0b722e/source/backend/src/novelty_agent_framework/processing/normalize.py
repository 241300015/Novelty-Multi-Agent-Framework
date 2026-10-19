"""源特有文本规整：OCR 与文本层各自清洗后收敛为统一规范文本。"""

from __future__ import annotations

import re
from collections import Counter

from ..schemas import PaperPage
from .clean import clean_text

_OCR_MARKER = re.compile(
    r"<\|ref\|>.*?<\|/ref\|>|<\|det\|>\[\[.*?\]\]<\|/det\|>",
    re.DOTALL,
)


def normalize_pages(pages: list[PaperPage], source: str) -> list[PaperPage]:
    """按来源执行规整，返回统一规范文本的页面列表。"""

    if source == "ocr":
        return [
            PaperPage(page=page.page, text=clean_text(normalize_ocr_text(page.text)))
            for page in pages
        ]

    if source == "mineru":
        return [
            PaperPage(page=page.page, text=clean_text(_clean_mineru_text(page.text)))
            for page in pages
        ]

    texts = [page.text for page in pages]
    texts = _strip_repeated_lines(texts)
    texts = [_join_hyphenated_lines(text) for text in texts]
    return [
        PaperPage(page=page.page, text=clean_text(text))
        for page, text in zip(pages, texts)
    ]


def normalize_ocr_text(text: str) -> str:
    """OCR 特有规整：剥离引用/坐标标记，修复明显重复片段。"""

    text = _OCR_MARKER.sub("", text)
    return _dedupe_repeats(text)


def _clean_mineru_text(text: str) -> str:
    """MinerU 文本规整：去掉图片链接、列表符号和明显的 Markdown 残留。"""

    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", text)
    text = re.sub(r"(?m)^\s*[-*+]\s+", "", text)
    text = _join_hyphenated_lines(text)
    return text


def _dedupe_repeats(text: str) -> str:
    """连续出现的相同 4-12 字符片段只保留一次（如“方法和方法”）。"""

    changed = True
    while changed:
        changed = False
        for size in range(12, 3, -1):
            new = re.sub(r"(.{%d})\1" % size, r"\1", text, count=1)
            if new != text:
                text = new
                changed = True
                break
    return text


def _strip_repeated_lines(texts: list[str]) -> list[str]:
    """去掉在多数页面重复出现的页眉/页脚短行。"""

    if not texts:
        return texts
    counter: Counter[str] = Counter()
    for text in texts:
        for line in text.splitlines():
            line = line.strip()
            if line and len(line) <= 30:
                counter[line] += 1
    threshold = max(2, int(len(texts) * 0.5))
    repeated = {line for line, count in counter.items() if count >= threshold}
    return [
        "\n".join(line for line in text.splitlines() if line.strip() not in repeated)
        for text in texts
    ]


def _join_hyphenated_lines(text: str) -> str:
    """英文断词接续：行尾连字符 + 换行 -> 直接拼接。"""

    return re.sub(r"-\s*\n\s*", "", text)
