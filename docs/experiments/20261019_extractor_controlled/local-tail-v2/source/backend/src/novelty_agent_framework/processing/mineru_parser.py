"""MinerU PDF 解析适配器（主应用侧）。

主应用不直接 import mineru，而是通过 subprocess 调用独立 mineru 环境中的
``scripts/mineru_worker.py``。本模块负责命令拼装、结果读取和把 MinerU 的
``content_list_v2.json`` / ``content_list.json`` 转成现有 ``PaperPage`` 结构。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from ..schemas import PaperEquation, PaperImage, PaperPage, PaperTable
from .textify import TextifyResult

DEFAULT_MINERU_ENV = "mineru"
DEFAULT_WORKER_PATH = Path("scripts/mineru_worker.py")


class MineruError(RuntimeError):
    """MinerU 解析失败或产物不完整。"""


@dataclass(frozen=True)
class MineruSettings:
    """MinerU worker 运行参数。"""

    python_path: str | None = None
    env_name: str = DEFAULT_MINERU_ENV
    worker_path: str | Path = DEFAULT_WORKER_PATH
    backend: str = "pipeline"
    method: str = "auto"
    lang: str = "ch"
    effort: str = "medium"
    timeout_seconds: int = 1800
    work_root: str | Path = "outputs/.mineru"
    model_source: str | None = None


class MineruParser:
    """调用独立 mineru 环境解析 PDF，并转换为项目内数据结构。"""

    def __init__(self, settings: MineruSettings | None = None) -> None:
        self.settings = settings or MineruSettings()

    def parse(self, source: str | Path, *, paper_id: str | None = None) -> TextifyResult:
        """解析 PDF，返回与 ``textify()`` 相同形状的 ``TextifyResult``。"""
        pdf_path = Path(source)
        if not pdf_path.exists():
            raise MineruError(f"PDF 不存在: {pdf_path}")

        work_dir = Path(self.settings.work_root) / (paper_id or pdf_path.stem)
        work_dir.mkdir(parents=True, exist_ok=True)
        manifest_path = work_dir / "mineru-manifest.json"

        command = self._build_command(pdf_path, work_dir, manifest_path)
        env = os.environ.copy()
        if self.settings.model_source:
            env["MINERU_MODEL_SOURCE"] = self.settings.model_source

        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=self.settings.timeout_seconds,
                env=env,
                check=False,
            )
        except FileNotFoundError as exc:
            raise MineruError(
                f"无法执行 MinerU 命令 {command[0]!r}，请确认 mineru 环境已创建"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise MineruError(
                f"MinerU 解析超时（>{self.settings.timeout_seconds}s）: {pdf_path}"
            ) from exc

        if not manifest_path.exists():
            detail = (completed.stdout or "")[-2000:]
            raise MineruError(
                f"MinerU worker 未生成 manifest；exit={completed.returncode}\n{detail}"
            )

        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not manifest.get("ok"):
            raise MineruError(f"MinerU 解析失败: {manifest.get('error', '未知错误')}")

        return self._to_textify_result(manifest, pdf_path)

    def _build_command(
        self,
        pdf_path: Path,
        work_dir: Path,
        manifest_path: Path,
    ) -> list[str]:
        worker_path = Path(self.settings.worker_path)
        if not worker_path.is_absolute():
            cwd_candidate = Path.cwd() / worker_path
            project_candidate = Path(__file__).resolve().parents[4] / worker_path
            worker_path = next(
                (path for path in (cwd_candidate, project_candidate) if path.exists()),
                cwd_candidate,
            )

        args = [
            "--input",
            str(pdf_path),
            "--output",
            str(work_dir),
            "--backend",
            self.settings.backend,
            "--method",
            self.settings.method,
            "--lang",
            self.settings.lang,
            "--effort",
            self.settings.effort,
            "--manifest",
            str(manifest_path),
        ]

        python = self._resolve_python()
        if python:
            return [*python, str(worker_path), *args]

        conda = shutil.which("conda")
        if conda:
            return [
                conda,
                "run",
                "--no-capture-output",
                "-n",
                self.settings.env_name,
                "python",
                str(worker_path),
                *args,
            ]
        raise MineruError("未找到 conda，无法定位 mineru 环境")

    def _resolve_python(self) -> list[str] | None:
        if self.settings.python_path:
            path = Path(self.settings.python_path)
            if path.exists():
                return [str(path)]
            # 允许传 "python3" 这类 PATH 命令。
            if shutil.which(self.settings.python_path):
                return [self.settings.python_path]
            raise MineruError(f"配置的 mineru_python 不存在: {path}")

        # 常见 conda 安装路径。
        conda_prefix = Path(os.environ.get("CONDA_PREFIX", ""))
        if conda_prefix.name == self.settings.env_name:
            candidate = conda_prefix / "python.exe"
            if candidate.exists():
                return [str(candidate)]

        base = Path(os.environ.get("CONDA_PREFIX", "")).parent
        candidates = [
            base / self.settings.env_name / "python.exe",
            Path.home() / "anaconda3" / "envs" / self.settings.env_name / "python.exe",
            Path.home() / "miniconda3" / "envs" / self.settings.env_name / "python.exe",
        ]
        for candidate in candidates:
            if candidate.exists():
                return [str(candidate)]
        return None

    def _to_textify_result(
        self,
        manifest: Mapping[str, Any],
        pdf_path: Path,
    ) -> TextifyResult:
        content_list_v2_path = manifest.get("content_list_v2")
        content_list_path = manifest.get("content_list")
        parse_dir = Path(manifest.get("parse_dir") or "")
        pages: list[PaperPage] = []
        images: list[PaperImage] = []
        tables: list[PaperTable] = []
        equations: list[PaperEquation] = []

        if content_list_v2_path and Path(content_list_v2_path).exists():
            data = json.loads(Path(content_list_v2_path).read_text(encoding="utf-8"))
            pages = _pages_from_v2(data)
            images, tables, equations = _structured_from_v2(data, parse_dir)
        elif content_list_path and Path(content_list_path).exists():
            data = json.loads(Path(content_list_path).read_text(encoding="utf-8"))
            pages = _pages_from_v1(data)
            images, tables, equations = _structured_from_v1(data, parse_dir)
        else:
            raise MineruError(
                f"MinerU 产物缺少 content_list: {pdf_path} manifest={manifest}"
            )

        if not pages:
            raise MineruError(f"MinerU 产物没有页面内容: {pdf_path}")

        return TextifyResult(
            pages=tuple(pages),
            source="mineru",
            warnings=(),
            images=tuple(images),
            tables=tuple(tables),
            equations=tuple(equations),
        )


def _pages_from_v2(data: Sequence[Any]) -> list[PaperPage]:
    """content_list_v2.json 是按页分组的 list[list[dict]]。"""
    pages: list[PaperPage] = []
    for page_idx, blocks in enumerate(data, start=1):
        if not isinstance(blocks, list):
            continue
        rendered = [
            part
            for block in blocks
            if block and (part := _render_v2_block(block))
        ]
        pages.append(PaperPage(page=page_idx, text="\n\n".join(rendered).strip()))
    return pages


def _pages_from_v1(data: Sequence[Any]) -> list[PaperPage]:
    """content_list.json 是扁平的 list[dict]，按 page_idx 聚合。"""
    grouped: dict[int, list[dict[str, Any]]] = {}
    for block in data:
        if not isinstance(block, dict):
            continue
        page_idx = int(block.get("page_idx", 0))
        grouped.setdefault(page_idx, []).append(block)

    pages: list[PaperPage] = []
    for page_idx in sorted(grouped):
        blocks = grouped[page_idx]
        rendered = [part for block in blocks if block and (part := _render_v1_block(block))]
        pages.append(PaperPage(page=page_idx + 1, text="\n\n".join(rendered).strip()))
    return pages


def _structured_from_v2(
    data: Sequence[Any],
    parse_dir: Path | None = None,
) -> tuple[list[PaperImage], list[PaperTable], list[PaperEquation]]:
    """从 content_list_v2 提取图片/表格/公式结构化块。"""
    images: list[PaperImage] = []
    tables: list[PaperTable] = []
    equations: list[PaperEquation] = []

    for page_idx, blocks in enumerate(data, start=1):
        if not isinstance(blocks, list):
            continue
        for index, block in enumerate(blocks):
            if not isinstance(block, dict):
                continue
            block_type = block.get("type", "")
            content = block.get("content") or {}
            if not isinstance(content, dict):
                content = {}

            if block_type in {"image", "chart", "seal"}:
                caption = _extract_text(
                    content.get(f"{block_type}_caption") or content.get("caption")
                )
                footnote = _extract_text(
                    content.get(f"{block_type}_footnote") or content.get("footnote")
                )
                image_source = content.get("image_source")
                source_path = (
                    image_source.get("path")
                    if isinstance(image_source, dict)
                    else None
                )
                path = _resolve_media_path(
                    parse_dir,
                    _first_text(
                        source_path,
                        content.get("image_path"),
                        content.get("img_path"),
                        content.get("path"),
                    ),
                )
                images.append(
                    PaperImage(
                        image_id=_make_block_id(block_type, page_idx, index),
                        kind=block_type,
                        page=page_idx,
                        path=path,
                        caption=caption,
                        footnote=footnote,
                        bbox=_bbox_to_list(block.get("bbox")),
                        raw=dict(block),
                    )
                )
            elif block_type == "table":
                body = _first_text(
                    content.get("table_body"),
                    content.get("table_body_html"),
                    content.get("body"),
                )
                caption = _extract_text(
                    content.get("table_caption") or content.get("caption")
                )
                footnote = _extract_text(
                    content.get("table_footnote") or content.get("footnote")
                )
                tables.append(
                    PaperTable(
                        table_id=_make_block_id("table", page_idx, index),
                        page=page_idx,
                        caption=caption,
                        footnote=footnote,
                        body=body,
                        body_format="html" if "<table" in body.lower() else "markdown",
                        bbox=_bbox_to_list(block.get("bbox")),
                        raw=dict(block),
                    )
                )
            elif block_type == "equation_interline":
                latex = _first_text(
                    content.get("math_content"),
                    content.get("latex"),
                    content.get("text"),
                )
                equations.append(
                    PaperEquation(
                        equation_id=_make_block_id("equation", page_idx, index),
                        page=page_idx,
                        latex=latex,
                        bbox=_bbox_to_list(block.get("bbox")),
                        raw=dict(block),
                    )
                )

    return images, tables, equations


def _structured_from_v1(
    data: Sequence[Any],
    parse_dir: Path | None = None,
) -> tuple[list[PaperImage], list[PaperTable], list[PaperEquation]]:
    """从 content_list.json（v1 扁平结构）提取图片/表格/公式。"""
    images: list[PaperImage] = []
    tables: list[PaperTable] = []
    equations: list[PaperEquation] = []

    for index, block in enumerate(data):
        if not isinstance(block, dict):
            continue
        block_type = block.get("type", "")
        page_idx = int(block.get("page_idx", 0)) + 1

        if block_type in {"image", "chart", "seal"}:
            path = _resolve_media_path(
                parse_dir,
                _first_text(
                    block.get("img_path"),
                    block.get("image_path"),
                    block.get("path"),
                ),
            )
            images.append(
                PaperImage(
                    image_id=_make_block_id(block_type, page_idx, index),
                    kind=block_type,
                    page=page_idx,
                    path=path,
                    caption=_extract_text(block.get(f"{block_type}_caption")),
                    footnote=_extract_text(block.get(f"{block_type}_footnote")),
                    bbox=_bbox_to_list(block.get("bbox")),
                    raw=dict(block),
                )
            )
        elif block_type == "table":
            tables.append(
                PaperTable(
                    table_id=_make_block_id("table", page_idx, index),
                    page=page_idx,
                    caption=_extract_text(block.get("table_caption")),
                    footnote=_extract_text(block.get("table_footnote")),
                    body=_first_text(block.get("table_body"), block.get("body")),
                    body_format="html"
                    if "<table" in str(block.get("table_body", "")).lower()
                    else "markdown",
                    bbox=_bbox_to_list(block.get("bbox")),
                    raw=dict(block),
                )
            )
        elif block_type == "equation":
            equations.append(
                PaperEquation(
                    equation_id=_make_block_id("equation", page_idx, index),
                    page=page_idx,
                    latex=_first_text(block.get("text"), block.get("latex")),
                    bbox=_bbox_to_list(block.get("bbox")),
                    raw=dict(block),
                )
            )

    return images, tables, equations


def _make_block_id(kind: str, page: int, index: int) -> str:
    return f"{kind}-{page}-{index + 1}"


def _bbox_to_list(value: Any) -> list[float]:
    if not isinstance(value, list):
        return []
    result: list[float] = []
    for item in value:
        try:
            result.append(float(item))
        except (TypeError, ValueError):
            continue
    return result


def _first_text(*values: Any) -> str:
    for value in values:
        text = _extract_text(value)
        if text:
            return text
    return ""


def _resolve_media_path(parse_dir: Path | None, path: str) -> str:
    """把 MinerU 产物中的相对媒体路径解析为绝对路径，便于后续复制。"""
    if not path:
        return ""
    candidate = Path(path)
    if candidate.is_absolute():
        return str(candidate)
    if parse_dir is not None:
        resolved = parse_dir / path
        if resolved.exists():
            return str(resolved.resolve())
    return path


def _render_v2_block(block: Mapping[str, Any]) -> str:
    block_type = block.get("type", "")
    content = block.get("content") or {}
    if not isinstance(content, dict):
        return ""

    if block_type == "title":
        level = int(content.get("level", 1) or 1)
        title = _extract_text(content.get("title_content"))
        return f"{'#' * min(level, 6)} {title}".strip() if title else ""
    if block_type == "paragraph":
        return _extract_text(content.get("paragraph_content"))
    if block_type == "equation_interline":
        math = _extract_text(content.get("math_content"))
        return f"$$\n{math}\n$$" if math else ""
    if block_type in {"code", "algorithm"}:
        key = "code_content" if block_type == "code" else "algorithm_content"
        body = _extract_text(content.get(key))
        caption = _extract_text(content.get(f"{block_type}_caption"))
        parts = [caption, f"```\n{body}\n```"] if body else [caption]
        return "\n\n".join(part for part in parts if part)
    if block_type in {"list", "index"}:
        items = content.get("list_items") or []
        rendered = []
        for item in items:
            if isinstance(item, dict):
                item_text = _extract_text(
                    item.get("item_content") or item.get("content")
                )
            else:
                item_text = str(item)
            if item_text:
                rendered.append(f"- {item_text}")
        return "\n".join(rendered)
    if block_type in {"image", "chart", "table", "seal"}:
        caption = _extract_text(content.get(f"{block_type}_caption"))
        footnote = _extract_text(content.get(f"{block_type}_footnote"))
        return "\n\n".join(part for part in (caption, footnote) if part)
    if block_type.startswith("page_"):
        return ""
    return _extract_text(content)


def _render_v1_block(block: Mapping[str, Any]) -> str:
    block_type = block.get("type", "")
    if block_type.startswith("page_") or block_type in {
        "header",
        "footer",
        "aside_text",
        "page_footnote",
    }:
        return ""

    text = block.get("text", "")
    if text:
        level = block.get("text_level")
        if isinstance(level, int) and level > 0:
            return f"{'#' * min(level, 6)} {text}".strip()
        return str(text).strip()

    if block_type == "table":
        caption = _extract_text(block.get("table_caption"))
        body = block.get("table_body", "")
        parts = [caption, body] if body else [caption]
        return "\n\n".join(part for part in parts if str(part).strip())
    if block_type in {"image", "chart"}:
        return _extract_text(block.get(f"{block_type}_caption"))
    if block_type == "equation":
        return block.get("text", "")
    if block_type == "list":
        items = block.get("items") or block.get("text", "")
        if isinstance(items, list):
            return "\n".join(f"- {item}" for item in items if str(item).strip())
        return str(items).strip()
    return ""


def _extract_text(value: Any) -> str:
    """从 content_list 常见的字符串/字典/列表嵌套中提取纯文本。"""
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        for key in ("content", "text"):
            if key in value:
                return _extract_text(value[key])
        return ""
    if isinstance(value, list):
        parts = []
        for item in value:
            text = _extract_text(item)
            if text:
                parts.append(text)
        return " ".join(parts).strip()
    return str(value).strip()
