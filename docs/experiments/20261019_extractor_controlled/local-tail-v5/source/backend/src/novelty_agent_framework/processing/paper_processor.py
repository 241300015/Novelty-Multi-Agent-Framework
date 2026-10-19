"""论文处理编排：textify → normalize → sections → title → PaperDocument。"""

from __future__ import annotations

import re
from pathlib import Path

from backend.env import ModelClient

from ..ports import PaperProcessor as PaperProcessorProtocol
from ..schemas import PaperDocument, PaperInput
from . import normalize, sections as sections_module, textify
from .mineru_parser import MineruParser, MineruSettings
from .title import extract_title_from_lines, extract_title_llm

DEFAULT_MINERU_SETTINGS = MineruSettings()


class DefaultPaperProcessor(PaperProcessorProtocol):
    """默认论文处理器：MinerU 优先，文本层/OCR 兜底，输出结构化文档。"""

    def __init__(
        self,
        *,
        ocr_client: ModelClient | None = None,
        llm_client: ModelClient | None = None,
        dpi: int = 200,
        min_chars_per_page: int = 200,
        parser: str = "mineru",
        mineru_parser: MineruParser | None = None,
        mineru_settings: MineruSettings | None = None,
    ) -> None:
        self.ocr_client = ocr_client
        self.llm_client = llm_client
        self.dpi = dpi
        self.min_chars_per_page = min_chars_per_page
        self.parser = parser
        self.mineru_parser = mineru_parser or MineruParser(
            mineru_settings or DEFAULT_MINERU_SETTINGS
        )

    def process(
        self,
        source: str | Path,
        *,
        force_ocr: bool = False,
        paper_id: str | None = None,
    ) -> PaperDocument:
        path = Path(source)
        paper_id = paper_id or path.stem

        warnings: list[str] = []
        result = self._try_mineru(path, paper_id=paper_id, warnings=warnings)
        if result is None:
            result = textify.textify(
                path,
                force_ocr=force_ocr,
                ocr_client=self.ocr_client,
                dpi=self.dpi,
                min_chars_per_page=self.min_chars_per_page,
            )
        warnings.extend(result.warnings)

        pages = normalize.normalize_pages(list(result.pages), result.source)
        marked = textify.assemble_marked_text(pages)
        sections, section_warnings = sections_module.split_sections(marked)
        references = sections_module.listify_references(sections.get("reference", ""))
        english_abstract = sections.get("english_abstract", "")
        keywords_zh = sections_module.parse_keywords_zh(sections.get("keywords", ""))
        keywords_en = sections_module.extract_keywords_en(english_abstract)

        title, title_warnings = self._extract_title(path, pages, result.source)
        warnings = warnings + section_warnings + title_warnings
        if not title:
            title = paper_id
            warnings.append("title: 未提取到标题，回退为 paper_id")

        return PaperDocument(
            paper_id=paper_id,
            title=title,
            abstract=sections.get("abstract", ""),
            english_abstract=english_abstract,
            full_text=marked,
            references=references,
            claimed_contributions=[],
            keywords_zh=keywords_zh,
            keywords_en=keywords_en,
            metadata={"language": _detect_language(marked)},
            sections=sections,
            pages=pages,
            source=result.source,
            parse_warnings=warnings,
            images=list(result.images),
            tables=list(result.tables),
            equations=list(result.equations),
        )

    def _try_mineru(
        self,
        path: Path,
        *,
        paper_id: str,
        warnings: list[str],
    ):
        """尝试 MinerU 解析；失败或质量不足时返回 None，由调用方走兜底。"""
        if self.parser not in ("mineru", "auto"):
            return None
        try:
            result = self.mineru_parser.parse(path, paper_id=paper_id)
        except Exception as exc:  # noqa: BLE001 - MinerU 不可用必须回退
            warnings.append(f"mineru: 解析失败，回退 textify：{exc}")
            return None

        pages = list(result.pages)
        if not _quality_ok_pages(pages, self.min_chars_per_page):
            warnings.append("mineru: 解析质量不足，回退 textify")
            return None
        return result

    def to_paper_input(self, document: PaperDocument) -> PaperInput:
        metadata = dict(document.metadata)
        metadata["source"] = document.source
        metadata["pages"] = str(len(document.pages))
        metadata["parse_warnings"] = "; ".join(document.parse_warnings)
        return PaperInput(
            paper_id=document.paper_id,
            title=document.title or document.paper_id,
            abstract=document.abstract,
            english_abstract=document.english_abstract,
            full_text=document.full_text,
            references=list(document.references),
            claimed_contributions=list(document.claimed_contributions),
            keywords_zh=list(document.keywords_zh),
            keywords_en=list(document.keywords_en),
            metadata=metadata,
            images=list(document.images),
            tables=list(document.tables),
            equations=list(document.equations),
        )

    def _extract_title(
        self,
        path: Path,
        pages: list,
        source: str,
    ) -> tuple[str, list[str]]:
        if not pages:
            return "", ["title: 无页面可提取"]
        first_text = pages[0].text
        if source == "text_layer":
            candidates = _font_based_candidates(path, first_text)
        else:
            candidates = _ocr_candidates(first_text)

        title = extract_title_from_lines(candidates)
        warnings: list[str] = []
        if not title:
            if self.llm_client is not None:
                try:
                    title = extract_title_llm(self.llm_client, first_text)
                except Exception as exc:
                    warnings.append(f"title: LLM 兜底失败：{exc}")
            else:
                warnings.append("title: 正则未命中且未配置 LLM 兜底")
        return title or "", warnings


def _quality_ok_pages(pages: list, min_chars_per_page: int) -> bool:
    """MinerU 结果质量门：有页、平均字符数达标、低字符页占比不超标。"""
    if not pages:
        return False
    counts = [len(page.text.strip()) for page in pages]
    mean = sum(counts) / len(counts)
    low_ratio = sum(1 for count in counts if count < min_chars_per_page) / len(counts)
    return mean >= min_chars_per_page and low_ratio <= 0.3


def _font_based_candidates(path: Path, first_text: str) -> list[str]:
    """文本层：保留原行序，只取字号不低于中位数 0.9 倍的行作为候选。"""

    import pymupdf

    doc = pymupdf.open(str(path))
    try:
        data = doc[0].get_text("dict")
        lines: list[tuple[float, str]] = []
        for block in data.get("blocks", []):
            for line in block.get("lines", []):
                spans = line.get("spans", [])
                if not spans:
                    continue
                size = max(span["size"] for span in spans)
                text = "".join(span["text"] for span in spans).strip()
                if text:
                    lines.append((size, text))
        if not lines:
            return [first_text]
        sizes = sorted(size for size, _ in lines)
        median = sizes[len(sizes) // 2]
        return [text for size, text in lines if size >= median * 0.9]
    finally:
        doc.close()


def _ocr_candidates(first_text: str) -> list[str]:
    lines = [line.strip() for line in first_text.splitlines()]
    headings = [
        line.lstrip("#").strip()
        for line in lines
        if line.startswith("#") and not re.search(r"(?i)^#\s*Page\s*\d+", line)
    ]
    return headings + lines


def _detect_language(text: str) -> str:
    return "zh-CN" if re.search(r"[\u4e00-\u9fff]", text) else "en"
