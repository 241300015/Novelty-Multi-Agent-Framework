#!/usr/bin/env python3
"""修复历史遗留的 CRLF 制品，使其与 Manifest 声明的 sha256 重新一致。

背景：``ReferenceStore.write_document`` 曾经以文本模式写文件，Windows 上会把
``\\n`` 翻译成 ``\\r\\n``，于是落盘字节与调用方按内存字符串算出的 sha256 不再
一致，``verify_artifact_file`` 抛 ``sha256 mismatch``、reader 一个字符也读不出来。
写入端已经修好（显式 ``newline="\\n"``），但**在此之前写下的制品仍然坏在盘上**：
实测某个工作区 177 个制品里有 84 个（47%）因此不可读，直接表现为 reader 大面积
失败与有效证据卡流失。

本脚本是**无损**的：Manifest 里的 sha256/byte_size 本就是按 LF 内容算出来的，
把文件中的 ``\\r\\n`` 规范化回 ``\\n`` 之后字节与声明完全一致，因此**不需要改动
Manifest**。每个文件在改写前都会先验证这一点，验证不通过的一律拒绝而不是猜测。

默认只检查（dry-run），加 ``--apply`` 才真正改写。只写 ``outputs/`` 下的制品。

用法::

    python scripts/repair_artifact_line_endings.py outputs/MG19333vrw
    python scripts/repair_artifact_line_endings.py outputs/MG19333vrw --apply
    python scripts/repair_artifact_line_endings.py outputs/*/ --apply
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "backend" / "src"))

from novelty_agent_framework.schemas import ReferenceNamespace  # noqa: E402

_NAMESPACE_DIRS = {
    ReferenceNamespace.RESEARCH: "references",
    ReferenceNamespace.SUBJECT_REFERENCE: "subject_references",
}


def _atomic_write_bytes(path: Path, payload: bytes) -> None:
    handle = tempfile.NamedTemporaryFile(
        mode="wb", dir=path.parent, prefix=f".{path.name}.", suffix=".tmp", delete=False
    )
    temporary = Path(handle.name)
    try:
        with handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def inspect_workspace(workspace: Path, *, apply: bool = False) -> dict[str, Any]:
    """检查（并可选地修复）一个 ``outputs/<paper_id>`` 工作区。"""

    report: dict[str, Any] = {
        "workspace": str(workspace),
        "namespaces": {},
        "repaired": 0,
        "unrepairable": [],
    }

    for namespace, directory in _NAMESPACE_DIRS.items():
        list_path = workspace / directory / "list.json"
        if not list_path.is_file():
            continue
        try:
            payload = json.loads(list_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            report["namespaces"][namespace.value] = {
                "error": f"{type(exc).__name__}: {exc}"
            }
            continue

        stats = {
            "artifacts": 0,
            "ok": 0,
            "repairable": 0,
            "repaired": 0,
            "missing_file": 0,
            "unrepairable": 0,
        }
        for artifact in payload.get("artifacts", []):
            stats["artifacts"] += 1
            artifact_id = artifact.get("artifact_id")
            declared = artifact.get("sha256")
            relative = artifact.get("relative_path")
            if not artifact_id or not declared or not relative:
                stats["unrepairable"] += 1
                report["unrepairable"].append(
                    {
                        "namespace": namespace.value,
                        "artifact_id": artifact_id,
                        "reason": "manifest entry lacks artifact_id/sha256/relative_path",
                    }
                )
                continue

            file_path = workspace / directory / relative
            if not file_path.is_file():
                stats["missing_file"] += 1
                continue
            raw = file_path.read_bytes()
            if hashlib.sha256(raw).hexdigest() == declared:
                stats["ok"] += 1
                continue

            normalized = raw.replace(b"\r\n", b"\n")
            if hashlib.sha256(normalized).hexdigest() != declared:
                stats["unrepairable"] += 1
                report["unrepairable"].append(
                    {
                        "namespace": namespace.value,
                        "artifact_id": artifact_id,
                        "reason": "CRLF 规范化后仍与声明的 sha256 不一致",
                        "on_disk_bytes": len(raw),
                        "declared_bytes": artifact.get("byte_size"),
                    }
                )
                continue

            stats["repairable"] += 1
            if apply:
                _atomic_write_bytes(file_path, normalized)
                stats["repaired"] += 1
                report["repaired"] += 1

        report["namespaces"][namespace.value] = stats

    report["ok"] = not report["unrepairable"]
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("workspaces", nargs="+", type=Path)
    parser.add_argument(
        "--apply", action="store_true", help="真正改写文件（缺省只检查）"
    )
    parser.add_argument("--compact", action="store_true", help="输出单行 JSON")
    args = parser.parse_args()

    reports = [
        inspect_workspace(workspace, apply=args.apply) for workspace in args.workspaces
    ]
    print(
        json.dumps(
            reports if len(reports) > 1 else reports[0],
            ensure_ascii=False,
            indent=None if args.compact else 2,
            sort_keys=True,
        )
    )
    return 0 if all(item["ok"] for item in reports) else 1


if __name__ == "__main__":
    raise SystemExit(main())
