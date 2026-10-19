"""查新工作流的命令行演示入口。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Sequence

from .schemas import PaperInput
from .workflows import NoveltyWorkflow


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="运行论文查新 Multi-Agent 离线演示")
    parser.add_argument("--input", type=Path, required=True, help="UTF-8 JSON 论文输入")
    parser.add_argument("--output", type=Path, help="可选的 UTF-8 JSON 输出路径")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    payload = json.loads(args.input.read_text(encoding="utf-8"))
    paper = PaperInput.model_validate(payload)

    workflow = NoveltyWorkflow.default()
    result_json = workflow.run(paper).model_dump_json(indent=2)

    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(result_json + "\n", encoding="utf-8")
    else:
        print(result_json)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
