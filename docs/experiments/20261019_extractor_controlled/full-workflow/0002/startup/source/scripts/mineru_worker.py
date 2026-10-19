"""MinerU PDF 解析 worker（在独立 mineru conda 环境运行）。

主应用（langgraph 环境）通过 subprocess 调用本脚本；本脚本只依赖 MinerU
及其自带依赖，不依赖 novelty_agent_framework。

用法示例:
    python scripts/mineru_worker.py \
      --input examples/MF2033k6lC.pdf \
      --output outputs/.mineru \
      --backend pipeline \
      --method auto \
      --manifest outputs/.mineru/manifest.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path


def _resolve_parse_dir(
    output_dir: Path,
    stem: str,
    backend: str,
    method: str,
) -> Path:
    """定位 MinerU 实际产物目录，找不到时回退到递归搜索。"""
    try:
        from mineru.cli.output_paths import resolve_parse_dir

        candidate = resolve_parse_dir(
            output_dir,
            stem,
            backend,
            method,
            is_office=False,
        )
        if candidate.exists():
            return candidate
    except Exception:
        pass

    # 优先找真正包含 Markdown 产物的目录（method=auto 时实际可能是 txt/ocr）。
    for path in output_dir.rglob("*.md"):
        if path.is_file() and path.parent.name == stem:
            return path.parent
    for path in output_dir.rglob("*.md"):
        if path.is_file():
            return path.parent

    # 最后退回按文件名找目录。
    matches = [
        path
        for path in output_dir.rglob("*")
        if path.is_dir() and path.name == stem
    ]
    if matches:
        return matches[0]

    raise FileNotFoundError(f"无法在 {output_dir} 下定位 MinerU 产物目录")


def _find_first(parse_dir: Path, patterns: tuple[str, ...]) -> Path:
    for pattern in patterns:
        matches = list(parse_dir.glob(pattern))
        if matches:
            return matches[0]
    raise FileNotFoundError(f"在 {parse_dir} 下找不到 {patterns} 任一文件")


async def _parse(args: argparse.Namespace) -> dict:
    from mineru.cli.client import run_orchestrated_cli

    input_path = Path(args.input)
    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)

    await run_orchestrated_cli(
        input_path=input_path,
        output_dir=output_dir,
        method=args.method,
        backend=args.backend,
        lang=args.lang,
        server_url=None,
        api_url=None,
        start_page_id=0,
        end_page_id=None,
        formula_enable=True,
        table_enable=True,
        image_analysis=True,
        client_side_output_generation=False,
        effort=args.effort,
        extra_cli_args=(),
    )

    parse_dir = _resolve_parse_dir(output_dir, input_path.stem, args.backend, args.method)
    markdown = _find_first(parse_dir, (f"{input_path.stem}.md", "*.md"))
    content_list_v2 = _find_first(
        parse_dir,
        (f"{input_path.stem}_content_list_v2.json", "*_content_list_v2.json"),
    )
    content_list = _find_first(
        parse_dir,
        (f"{input_path.stem}_content_list.json", "*_content_list.json"),
    )

    return {
        "ok": True,
        "parse_dir": str(parse_dir),
        "markdown": str(markdown),
        "content_list_v2": str(content_list_v2),
        "content_list": str(content_list),
        "backend": args.backend,
        "method": args.method,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="MinerU PDF 解析 worker")
    parser.add_argument("--input", required=True, help="PDF 文件路径")
    parser.add_argument("--output", required=True, help="MinerU 输出根目录")
    parser.add_argument("--backend", default="pipeline", help="pipeline / hybrid-engine / vlm-engine")
    parser.add_argument("--method", default="auto", choices=["auto", "txt", "ocr"])
    parser.add_argument("--lang", default="ch", help="OCR 语言，pipeline 后端使用")
    parser.add_argument("--effort", default="medium", help="hybrid 后端 effort")
    parser.add_argument("--manifest", required=True, help="结果 manifest JSON 路径")
    args = parser.parse_args()

    try:
        result = asyncio.run(_parse(args))
    except Exception as exc:  # noqa: BLE001 - worker 必须把任何失败写回 manifest
        result = {"ok": False, "error": str(exc)}

    manifest = Path(args.manifest)
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        json.dumps(result, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
