"""标题提取：行级正则优先，失败时走 LLM 兜底。"""

from __future__ import annotations

import re

from backend.env import ChatMessage, ModelClient

TITLE_PROMPT = (
    "从以下论文首页文本中提取论文标题。只输出标题本身，不要任何解释或标点装饰。\n\n{text}"
)

_TEMPLATE_KEYWORDS = (
    "论文题目",
    "研究生",
    "学位",
    "摘要",
    "ABSTRACT",
    "大学",
    "学院",
    "导师",
    "指导教师",
    "姓名",
    "日期",
    "学号",
    "专业",
    "研究方向",
    "关键词",
    "Key words",
    "Keywords",
)


def extract_title_from_lines(candidates: list[str]) -> str | None:
    """从候选行中挑标题：排除模板行，相邻可接受行合并（支持跨行标题）。

    取第一个合并完成的候选（标题通常位于首页靠前位置）。
    """

    def acceptable(line: str) -> bool:
        if len(line) < 4:
            return False
        if any(keyword in line for keyword in _TEMPLATE_KEYWORDS):
            return False
        if re.fullmatch(r"[\d\s\W]+", line):
            return False
        return True

    current: list[str] = []
    for line in candidates:
        line = line.strip().lstrip("#").strip()
        if acceptable(line) and len("".join(current)) + len(line) <= 60:
            current.append(line)
        elif current:
            return "".join(current).strip()
    if current:
        return "".join(current).strip()
    return None


def extract_title_llm(client: ModelClient, text: str) -> str:
    response = client.complete(
        [ChatMessage(role="user", content=TITLE_PROMPT.format(text=text[:2000]))]
    )
    return (response.content or "").strip().strip("\"'")
