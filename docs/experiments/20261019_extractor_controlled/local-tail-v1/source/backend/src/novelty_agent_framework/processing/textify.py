"""PDF 文本化：文本层优先，质量不足时走 DeepSeek-OCR 多模态识别。"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

import pymupdf

from backend.env import ChatMessage, ImageContentPart, ModelClient

from ..schemas import PaperEquation, PaperImage, PaperPage, PaperTable

OCR_PROMPT = (
    "请识别图片中的全部文字并输出为 markdown，保留段落与结构，不要省略任何文字。"
)


@dataclass(frozen=True)
class TextifyResult:
    pages: tuple[PaperPage, ...]
    source: str  # "text_layer" | "ocr" | "mineru"
    warnings: tuple[str, ...]
    images: tuple[PaperImage, ...] = ()
    tables: tuple[PaperTable, ...] = ()
    equations: tuple[PaperEquation, ...] = ()


def assemble_marked_text(pages: Sequence[PaperPage]) -> str:
    """按页拼接带 ``# Page N`` 标记的全文。"""

    parts = [f"\n\n# Page {page.page}\n{page.text}" for page in pages]
    return "".join(parts).strip()


def textify(
    source: str | Path,
    *,
    force_ocr: bool = False,
    ocr_client: ModelClient | None = None,
    dpi: int = 200,
    min_chars_per_page: int = 200,
) -> TextifyResult:
    """文本化 PDF：文本层质量达标则直接使用，否则降级 OCR。"""

    doc = pymupdf.open(str(source))
    try:
        if not force_ocr:
            pages = _extract_text_layer(doc)
            if _quality_ok(pages, min_chars_per_page):
                return TextifyResult(tuple(pages), "text_layer", ())
            warnings = ["文本层质量不足（存在低字符数页面），降级使用 OCR"]
        else:
            pages = []
            warnings = []

        if ocr_client is None:
            raise ValueError("OCR 路径需要注入 ocr_client（ModelClient）")
        ocr_pages = _extract_ocr(doc, ocr_client, dpi=dpi, warnings=warnings)
        return TextifyResult(tuple(ocr_pages), "ocr", tuple(warnings))
    finally:
        doc.close()


def _extract_text_layer(doc: pymupdf.Document) -> list[PaperPage]:
    pages = []
    for index in range(doc.page_count):
        text = doc[index].get_text("text") or ""
        pages.append(PaperPage(page=index + 1, text=text))
    return pages


def _quality_ok(pages: list[PaperPage], min_chars_per_page: int) -> bool:
    """质量门：平均字符数达标且低字符页占比不超过 30%。"""

    if not pages:
        return False
    counts = [len(page.text.strip()) for page in pages]
    mean = sum(counts) / len(counts)
    low_ratio = sum(1 for count in counts if count < min_chars_per_page) / len(counts)
    return mean >= min_chars_per_page and low_ratio <= 0.3


def _extract_ocr(
    doc: pymupdf.Document,
    client: ModelClient,
    *,
    dpi: int,
    warnings: list[str],
) -> list[PaperPage]:
    pages = []
    for index in range(doc.page_count):
        png = doc[index].get_pixmap(dpi=dpi).tobytes("png")
        data_url = "data:image/png;base64," + base64.b64encode(png).decode("ascii")
        try:
            response = client.complete(
                [
                    ChatMessage(
                        role="user",
                        content=[ImageContentPart(image_url=data_url), OCR_PROMPT],
                    )
                ]
            )
            text = (response.content or "").strip()
        except Exception as exc:
            warnings.append(f"第 {index + 1} 页 OCR 失败：{exc}")
            text = f"Error on page {index + 1}"
        pages.append(PaperPage(page=index + 1, text=text))
    return pages
