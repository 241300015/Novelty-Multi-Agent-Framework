"""通用文本清洗规则（两种来源共用）。"""

from __future__ import annotations

import re

PAGE_MARKER_PATTERN = re.compile(r"#\s*Page\s*\d+", re.IGNORECASE)


def clean_text(text: str) -> str:
    """通用清洗：空白、页码标记、多余空行。"""

    if not text:
        return ""
    text = re.sub(r" +", " ", text)
    text = re.sub(r"\n\s+", "\n", text)
    text = PAGE_MARKER_PATTERN.sub("", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def clean_section(text: str, section_type: str) -> str:
    """章节级清洗：通用清洗 + 章节标题残留剥离。"""

    text = clean_text(text)
    if section_type == "abstract":
        text = re.sub(r"(?m)^\s*中文摘要\s*$", "", text)
    if section_type == "english_abstract":
        text = re.sub(r"(?im)^\s*(?:英文摘要|Abstract)\s*$", "", text)
    if section_type == "conclusion":
        text = re.sub(
            r"(?m)^\s*第[一二三四五六七八九十\d]+章\s*(?:结论|总结|总结与展望|展望)\s*$",
            "",
            text,
        )
    if section_type == "reference":
        text = re.sub(r"(?m)^\s*参考文献\s*$", "", text)
        text = re.sub(r"(?m)^\s*\d{1,4}\s*$", "", text)  # 引用页页码
    return text.strip()
