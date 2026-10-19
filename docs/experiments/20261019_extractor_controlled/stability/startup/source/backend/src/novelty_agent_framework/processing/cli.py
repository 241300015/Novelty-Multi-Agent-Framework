"""论文处理 CLI：把 PDF 处理为工作流兼容的 paper.json。"""

from __future__ import annotations

import argparse
from pathlib import Path

from ..config import build_model_registry, load_config
from ..persistence import persist_paper_input
from .mineru_parser import MineruSettings
from .paper_processor import DefaultPaperProcessor


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="把论文 PDF 处理为工作流兼容的 paper.json")
    parser.add_argument("--input", type=Path, required=True, help="PDF 文件路径")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("outputs"),
        help="Paper 工作目录根路径（默认 outputs/）",
    )
    parser.add_argument("--paper-id", default=None, help="覆盖 paper_id（默认取 PDF 文件名）")
    parser.add_argument("--force-ocr", action="store_true", help="强制走 DeepSeek-OCR 路径")
    parser.add_argument(
        "--parser",
        choices=["mineru", "text_layer", "ocr", "auto"],
        default=None,
        help="PDF 解析器：mineru 优先，text_layer/ocr 强制旧路径（默认读配置）",
    )
    parser.add_argument("--mineru-python", default=None, help="mineru 环境 Python 路径")
    parser.add_argument("--mineru-backend", default=None, help="MinerU backend（pipeline 等）")
    parser.add_argument("--mineru-method", default=None, choices=["auto", "txt", "ocr"])
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    config = load_config()
    registry = build_model_registry(config)
    processing_cfg = config.get("processing", {})
    ocr_client = registry.client_for(processing_cfg.get("ocr_model", "deepseek-ocr"))
    llm_client = registry.client_for(processing_cfg.get("llm_model", "deepseek-flash"))

    mineru_settings = MineruSettings(
        python_path=args.mineru_python or processing_cfg.get("mineru_python"),
        env_name=processing_cfg.get("mineru_env", "mineru"),
        worker_path=processing_cfg.get("mineru_worker", "scripts/mineru_worker.py"),
        backend=args.mineru_backend or processing_cfg.get("mineru_backend", "pipeline"),
        method=args.mineru_method or processing_cfg.get("mineru_method", "auto"),
        lang=processing_cfg.get("mineru_lang", "ch"),
        effort=processing_cfg.get("mineru_effort", "medium"),
        timeout_seconds=int(processing_cfg.get("mineru_timeout_seconds", 1800)),
        work_root=processing_cfg.get("mineru_work_root", "outputs/.mineru"),
        model_source=processing_cfg.get("mineru_model_source"),
    )
    parser_mode = args.parser or processing_cfg.get("parser", "mineru")

    processor = DefaultPaperProcessor(
        ocr_client=ocr_client,
        llm_client=llm_client,
        dpi=int(processing_cfg.get("dpi", 200)),
        min_chars_per_page=int(
            processing_cfg.get("quality_min_chars_per_page", 200)
        ),
        parser=parser_mode,
        mineru_settings=mineru_settings,
    )
    document = processor.process(
        args.input,
        force_ocr=args.force_ocr,
        paper_id=args.paper_id,
    )
    compatible = processor.to_paper_input(document)

    workspace = persist_paper_input(document, compatible, output_root=args.output)
    print(f"Paper 工作目录：{workspace}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
