#!/usr/bin/env python3
"""Build the zero-call evidence package for NSTAB-SP-20260917.

This is deliberately an archive reader, not a workflow entry point.  It never
constructs a provider or model client, and it only writes the supplied output
directory.  The historical archives contain compiled ``SearchPlan`` objects,
but not the original ``SearchPlanDraft`` model responses; consequently every
input used by the compiler comparison is labelled ``derived_projection``.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import subprocess
from datetime import datetime, timezone
from itertools import combinations
from pathlib import Path
from typing import Any


CASE_ID = "MF2033k6lC_NP2_first_round"
EXPERIMENT_ID = "NSTAB-SP-20260917"
RUNS = ("0004", "0005", "0006")


def _read(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _path_hash(path: Path) -> str:
    """Hash a file, or a deterministic listing when an artifact is a directory."""

    if path.is_file():
        return _sha256(path)
    listing = [
        {"path": item.relative_to(path).as_posix(), "sha256": _sha256(item)}
        for item in sorted(path.rglob("*")) if item.is_file()
    ]
    return _canonical_hash(listing)


def _canonical_hash(value: Any) -> str:
    data = json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8")


def _write_csv(path: Path, rows: list[dict[str, Any]], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def _relative(repo: Path, path: Path) -> str:
    return path.relative_to(repo).as_posix()


def _first_task_plan(retrieval_plans: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return NP-2/T-1's task and its archived compiled plan, or fail loudly."""

    for point in retrieval_plans.get("novelty_point_plans", []):
        if point.get("novelty_point_id") != "NP-2":
            continue
        task = next((item for item in point.get("research_tasks", [])
                     if item.get("task_id") == "T-1"), None)
        plan = next((item for item in point.get("search_plans", [])
                     if item.get("task_id") == "T-1"), None)
        if task and plan:
            return task, plan
    raise ValueError("NP-2/T-1 was not present in retrieval-plans.json")


def _point(novelty_points: Any) -> dict[str, Any]:
    points = novelty_points.get("novelty_points", novelty_points) if isinstance(novelty_points, dict) else novelty_points
    for point in points:
        if point.get("point_id") == "NP-2":
            return point
    raise ValueError("NP-2 was not present in novelty-points.json")


def _c1_pool(plan: dict[str, Any]) -> dict[str, Any]:
    """Create the bounded shadow pool: all concepts plus all two-concept ANDs.

    This implements the task-book's C1 rule exactly for up to six concepts.
    It does not add terms, aliases, or database syntax.  The caller is still
    responsible for adapting expressions for a concrete provider.
    """

    concept_ids = [concept["concept_id"] for concept in plan["concepts"]]
    if not 1 <= len(concept_ids) <= 6:
        raise ValueError("C1 supports one through six concepts; refusing importance truncation")
    combinations_to_compile = [tuple(concept_ids)]
    combinations_to_compile.extend(combinations(concept_ids, 2))
    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, ...]] = set()
    for index, ids in enumerate(combinations_to_compile, start=1):
        if ids in seen:
            continue
        seen.add(ids)
        form = "single_concept_degenerate" if len(ids) == 1 else (
            "full_conjunction" if len(ids) == len(concept_ids) else "pair_conjunction"
        )
        rows.append({
            "strategy_id": f"C1-{index:02d}",
            "level": "shadow",
            "expression": " AND ".join(ids),
            "concept_ids": list(ids),
            "form": form,
            "use_alias": True,
            "use_exclude": True,
            "database_adapter": "not_applied",
        })
    return {
        "variant": "C1_shadow_bounded_combinations_v1",
        "input_kind": "derived_projection",
        "concept_count": len(concept_ids),
        "strategies": rows,
        "rules": [{
            "stage": "concept_combination_generation",
            "rule_id": "full_plus_pairs",
            "input_concept_ids": concept_ids,
            "selected_concept_ids": concept_ids,
            "excluded_concept_ids": [],
            "selection_reason": "all_concepts_plus_all_unordered_pairs; no importance selection",
        }],
    }


def _inventory_row(repo: Path, run: str, task: dict[str, Any], kind: str,
                   path: Path | None, state: str, provenance: str,
                   supports: str, limits: str) -> dict[str, str]:
    return {
        "source_run_id": run,
        "point_id": "NP-2",
        "task_id": task.get("task_id", "T-1"),
        "parent_task_id": "",
        "task_stage": "first_round",
        "artifact_kind": kind,
        "relative_path": _relative(repo, path) if path and path.exists() else "",
        "content_hash": _path_hash(path) if path and path.exists() else "",
        "availability": state,
        "data_source": provenance,
        "supports": supports,
        "cannot_support": limits,
    }


def _git_identity(repo: Path, *, exclude_path: Path) -> dict[str, str]:
    def run(*args: str) -> str:
        result = subprocess.run(args, cwd=repo, text=True, capture_output=True,
                                check=False)
        return result.stdout.strip() if result.returncode == 0 else "unavailable"
    patch = run("git", "diff", "--binary", "HEAD")
    try:
        excluded = exclude_path.relative_to(repo).as_posix().rstrip("/") + "/"
    except ValueError:
        # Temporary output outside the repository cannot appear in git status.
        excluded = ""
    status_lines = [line for line in run("git", "status", "--porcelain").splitlines()
                    if not excluded or not line[3:].replace("\\", "/").startswith(excluded)]
    untracked = [line[3:] for line in status_lines if line.startswith("?? ")]
    untracked_bytes = b"".join(
        path.read_bytes() for path in sorted(repo / item for item in untracked)
        if path.is_file()
    )
    identity_bytes = patch.encode() + "\n".join(status_lines).encode() + untracked_bytes
    return {
        "commit": run("git", "rev-parse", "HEAD"),
        "worktree_patch_sha256": hashlib.sha256(identity_bytes).hexdigest(),
        "worktree_dirty": str(bool(status_lines)),
        "worktree_status": status_lines,
    }


def build(repo: Path, output: Path) -> None:
    archive = repo / "docs/experiments/20260916_233840/runs/full"
    if output.exists() and any(output.iterdir()):
        raise FileExistsError(f"refusing to overwrite a non-empty output directory: {output}")
    output.mkdir(parents=True, exist_ok=False)

    all_inventory: list[dict[str, str]] = []
    selected: dict[str, Any] | None = None
    comparisons: list[dict[str, Any]] = []
    for run in RUNS:
        workspace = archive / run / "MF2033k6lC"
        retrieval_path = workspace / "retrieval-plans.json"
        points_path = workspace / "novelty-points.json"
        attempt_path = workspace / "research-runs/NP-2/T-1/attempt-1.json"
        runtime_dirs = sorted((workspace / "runtime").glob("run-*"))
        task, plan = _first_task_plan(_read(retrieval_path))
        rows = [
            _inventory_row(repo, run, task, "compiled_search_plan", retrieval_path,
                           "available", "original",
                           "C0 archived plan and task binding",
                           "does not contain original SearchPlanDraft"),
            _inventory_row(repo, run, task, "novelty_point", points_path,
                           "available", "original", "fixed point input",
                           "does not prove planner prompt identity"),
            _inventory_row(repo, run, task, "research_result", attempt_path,
                           "available", "original", "execution/candidate lineage",
                           "does not reconstruct planner draft"),
            _inventory_row(repo, run, task, "runtime_debug", runtime_dirs[0] if runtime_dirs else None,
                           "available" if runtime_dirs else "missing", "original",
                           "stage and tool event inspection", "not a zero-call response fixture for new C1 requests"),
            _inventory_row(repo, run, task, "search_plan_draft", None, "missing",
                           "missing", "would support exact Draft x Compiler replay",
                           "P4/P5/P6 cannot be labelled original Drafts"),
        ]
        all_inventory.extend(rows)
        c0 = {"variant": "C0_archived_compiled_plan", "input_kind": "derived_projection",
              "task": task, "plan": plan,
              "rules": [{"stage": "archive_recovery", "rule_id": "compiled_plan_as_is",
                         "selection_reason": "original SearchPlanDraft absent; no recompilation claimed"}]}
        c1 = _c1_pool(plan)
        for variant, value in (("C0", c0), ("C1", c1)):
            unit = output / "compiler-comparison" / f"P{run[-1]}_{variant}"
            _write_json(unit / "query-pool.json", value)
            _write_json(unit / "compile-trace.json", value["rules"])
        c0_expressions = {item["expression"] for item in plan["strategies"]}
        c1_expressions = {item["expression"] for item in c1["strategies"]}
        comparisons.append({
            "historical_input": f"P{run[-1]}", "input_kind": "derived_projection",
            "c0_strategy_count": len(plan["strategies"]),
            "c0_dsl_count": len(c0_expressions),
            "c1_strategy_count": len(c1["strategies"]),
            "c1_dsl_count": len(c1_expressions),
            "c0_requests": "not_adapted",
            "c1_requests": "not_adapted",
            "executed_requests": "not_measured",
            "deduplicated_works": "not_measured",
            "concept_payload_hash": _canonical_hash(plan["concepts"]),
            "strategy_payload_hash": _canonical_hash(plan["strategies"]),
            "c0_only": sorted(c0_expressions - c1_expressions),
            "c1_only": sorted(c1_expressions - c0_expressions),
            "shared": sorted(c0_expressions & c1_expressions),
            "status": "completed_local_compile_only",
        })
        if run == "0004":
            selected = {"task": task, "plan": plan, "point": _point(_read(points_path)),
                        "workspace": workspace, "paths": {"retrieval": retrieval_path,
                                                             "point": points_path,
                                                             "attempt": attempt_path}}

    assert selected is not None
    fixture_dir = output / "fixtures/np2"
    _write_json(fixture_dir / "novelty-point.json", selected["point"])
    _write_json(fixture_dir / "research-task.json", selected["task"])
    initial_state = {"source": "archive", "mutable_state": "not_copied",
                     "read_only_assets": ["references/list.json", "subject_references/list.json"],
                     "note": "No new run was started; this package stops at query_pool."}
    _write_json(fixture_dir / "initial-state.json", initial_state)
    artifact_index = [{"kind": kind, "path": _relative(repo, path), "sha256": _sha256(path)}
                      for kind, path in selected["paths"].items()]
    _write_json(fixture_dir / "artifact-index.json", artifact_index)
    input_manifest = {"experiment_id": EXPERIMENT_ID, "case_id": CASE_ID,
                      "selected_source_run_id": "0004", "selection_rule": "first complete run (4, then 5, then 6)",
                      "input_kind": "mixed_original_and_derived_projection",
                      "hashes": {"novelty_point": _canonical_hash(selected["point"]),
                                 "research_task": _canonical_hash(selected["task"]),
                                 "compiled_plan": _canonical_hash(selected["plan"])} }
    _write_json(fixture_dir / "input-manifest.json", input_manifest)

    _write_csv(output / "source-inventory.csv", all_inventory, list(all_inventory[0]))
    manifest = {"experiment_id": EXPERIMENT_ID, "case_id": CASE_ID,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "network": {"allow_model_calls": False, "allow_provider_calls": False,
                            "allow_reader_fetch": False}, "budget": {"max_model_calls": 0,
                            "max_provider_requests": 0, "max_estimated_cost_rmb": 0.0},
                "code": _git_identity(repo, exclude_path=output), "archive_root": _relative(repo, archive),
                "selected_input": input_manifest}
    _write_json(output / "source-manifest.json", manifest)
    _write_json(output / "experiment-manifest.json", {**manifest, "mode": "compile_only",
                "compiler_variants": ["C0_archived_compiled_plan", "C1_shadow_bounded_combinations_v1"],
                "executor_variants": [], "stop_after": "query_pool"})

    matrix = []
    for item in comparisons:
        matrix.extend([
            {"unit": f"{item['historical_input']}_C0", "compiler": "C0", "executor": "not_run",
             "status": item["status"], "reason": "archived compiled plan inspected locally"},
            {"unit": f"{item['historical_input']}_C1", "compiler": "C1", "executor": "not_run",
             "status": item["status"], "reason": "shadow query pool compiled locally; no provider fixture replay"},
        ])
    _write_csv(output / "analysis/comparison-matrix.csv", matrix,
               ["unit", "compiler", "executor", "status", "reason"])
    _write_csv(output / "analysis/cost-ledger.csv", [{"unit": "Part0-Part2", "model_calls": 0,
                "provider_requests": 0, "reader_fetches": 0, "estimated_cost_rmb": "0.0"}],
               ["unit", "model_calls", "provider_requests", "reader_fetches", "estimated_cost_rmb"])
    _write_json(output / "analysis/evidence-findings.json", {"findings": comparisons,
                "conclusion": "C1 changes query opportunities deterministically, but no archived responses are claimed for new C1 expressions."})

    (output / "baseline.md").write_text(
        "# 基线与材料核对\n\n"
        "- 冻结当前代码身份见 `source-manifest.json`；历史输入来自 `20260916_233840/runs/full/0004–0006`。\n"
        "- 选择 run 4 的 NP-2/T-1：它是预定顺序中的首个完整样本，不按结果优劣选择。\n"
        "- 三次都有编译后的 SearchPlan、任务结果、Runtime Debug；原始 SearchPlanDraft 未归档。\n"
        "  因而 C0/C1 输入均为 `derived_projection`，不可宣称为精确历史 Draft × Compiler 重放。\n"
        "- 当前职责链：`agents/search_planner.py:SearchPlannerAgent.plan` 生成一次计划；"
        "`agents/search_plan_compiler.py:build_runtime_plan` 纯本地编译；"
        "`tools/database_search/structured_retrieval.py` 消费传入计划并只生成 fallback 链，"
        "不在工具内再次调用 Planner。任务级 Researcher 的工具注册位于 `agents/research.py`。\n"
        "- 本轮仅执行本地文件读取与表达式组合，模型、Provider、Reader 调用均为 0。\n", encoding="utf-8")
    comparison_lines = ["# Compiler comparison (zero-call)", "",
                        "C0 是归档中的已编译计划；C1 是全概念 AND 加所有两两 AND 的影子池。",
                        "C1 不使用 `importance` 进行选池，不新增词项，也没有应用数据库语法适配。", ""]
    for item in comparisons:
        comparison_lines.extend([f"## {item['historical_input']}", "",
            f"- C0 strategies: {item['c0_strategy_count']}; unique DSL expressions: {item['c0_dsl_count']}.",
            f"- C1 strategies: {item['c1_strategy_count']}; unique DSL expressions: {item['c1_dsl_count']}.",
            "- Adapted and executed request counts were not measured in this local comparison.",
            f"- Only C0: `{'; '.join(item['c0_only']) or 'none'}`.",
            f"- Only C1: `{'; '.join(item['c1_only']) or 'none'}`.",
            "- This establishes changed query opportunities only; it says nothing about live recall.", ""])
    (output / "compiler-comparison/compiler-comparison.md").write_text("\n".join(comparison_lines), encoding="utf-8")
    concept_variation = len({item["concept_payload_hash"] for item in comparisons}) > 1
    strategy_variation = len({item["strategy_payload_hash"] for item in comparisons}) > 1
    differences = []
    if concept_variation:
        differences.append("概念载荷")
    if strategy_variation:
        differences.append("策略完整载荷（含 expression、alias/exclude 开关）")
    observed = "、".join(differences) if differences else "无已测字段差异"
    (output / "report.md").write_text(
        "# 单查新点稳定性定位报告（Part 0–2）\n\n"
        f"已归档编译计划的字段差分：{observed}。"
        "这是一项直接观察，尚不能归因于 Planner，因为原始 Draft 和完整 Planner 输入未归档。\n\n"
        "C1 对每份派生计划确定性生成 16 条（6 概念时）组合，保留了 C0 选池未提供的组合机会。"
        "没有为这些新表达式补造响应；Executor、阅读、成卡和任何 live 单元均为 `not_run`。\n\n"
        "建议修改位置：先在 Planner→查询池边界保存原始 Draft、编译规则事件和数据库适配后的请求；"
        "随后才能用严格 fixture 键检查 C0/C1 的 Executor 差异。当前证据不支持修改 Reader、Builder 或生产默认编译器。\n",
        encoding="utf-8")
    (output / "solution.md").write_text(
        "# 实施记录\n\n"
        "执行：`python scripts/single_point_stability_audit.py --repo . --output <dir>`。\n\n"
        "该脚本不导入工作流、Provider 或模型客户端，只读取归档 JSON 并写入新目录。"
        "费用：0 模型调用、0 Provider 请求、0 Reader 获取。\n", encoding="utf-8")
    (output / "analysis/next-step.md").write_text(
        "# 下一步\n\n"
        "要运行 Part 3，先为 C0/C1 的实际数据库请求建立包含 adapter、排序、分页和过滤条件的严格 fixture 清单。"
        "缺少严格匹配响应时应记录 `replay_miss`，不得联网或计为零命中。\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    build(args.repo.resolve(), args.output.resolve())


if __name__ == "__main__":
    main()
