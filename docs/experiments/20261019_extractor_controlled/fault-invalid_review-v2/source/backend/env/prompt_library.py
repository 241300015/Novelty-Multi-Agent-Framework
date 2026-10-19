"""版本化 Prompt 的加载与渲染。

提示词以数据文件形式存放在 ``prompts/`` 目录，通过 front matter 携带
``name`` / ``version`` / ``system`` 元数据，正文是 user 消息模板。
Agent 只负责提供业务变量，不在路由或 LangGraph 节点中拼接提示词。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping


class PromptRenderError(RuntimeError):
    """提示词加载或渲染失败。"""


@dataclass(frozen=True)
class PromptTemplate:
    """一份未渲染的提示词模板。"""

    name: str
    version: str | None
    system: str
    user_template: str


@dataclass(frozen=True)
class RenderedPrompt:
    """渲染完成的 system / user 消息。"""

    name: str
    version: str | None
    system: str
    user: str


def parse_front_matter(text: str) -> tuple[dict[str, str], str]:
    """解析 ``---`` 包裹的简单 front matter，返回 (元数据, 正文)。"""

    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text

    end = None
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            end = index
            break
    if end is None:
        return {}, text

    meta: dict[str, str] = {}
    current_key: str | None = None
    current_block: list[str] = []
    for line in lines[1:end]:
        if line.startswith((" ", "\t")):
            if current_key is not None:
                current_block.append(line.strip())
            continue
        if current_key is not None:
            meta[current_key] = "\n".join(current_block).strip()
            current_key = None
            current_block = []
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        current_key = key.strip()
        value = value.strip()
        if value == "|":
            current_block = []
        else:
            meta[current_key] = value
            current_key = None
    if current_key is not None:
        meta[current_key] = "\n".join(current_block).strip()

    body = "\n".join(lines[end + 1 :]).strip()
    return meta, body


class PromptLibrary:
    """按名称加载并渲染版本化提示词模板。"""

    def __init__(
        self,
        root: Path | str,
        *,
        fallbacks: Mapping[str, PromptTemplate] | None = None,
    ) -> None:
        self._root = Path(root)
        self._fallbacks = dict(fallbacks or {})
        self._templates: dict[str, PromptTemplate] = {}

    def render(self, name: str, **variables: Any) -> RenderedPrompt:
        template = self._load(name)
        try:
            user = template.user_template.format(**variables)
        except KeyError as exc:
            raise PromptRenderError(
                f"提示词 {name} 缺少变量: {exc.args[0]}"
            ) from exc
        return RenderedPrompt(
            name=template.name,
            version=template.version,
            system=template.system,
            user=user,
        )

    def _load(self, name: str) -> PromptTemplate:
        if name in self._templates:
            return self._templates[name]

        template: PromptTemplate | None = None
        path = self._root / f"{name}.md"
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            template = self._fallbacks.get(name)
        else:
            meta, body = parse_front_matter(text)
            template = PromptTemplate(
                name=meta.get("name", name),
                version=meta.get("version"),
                system=meta.get("system", "").strip(),
                user_template=body.strip(),
            )

        if template is None:
            raise PromptRenderError(f"提示词文件不存在: {path}")
        self._templates[name] = template
        return template
