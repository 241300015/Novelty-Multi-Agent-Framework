"""Default 报告的阶段摘要与现有落盘文件目录，不推导新的业务裁定。"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import quote

# 名称、工作区相对路径、报告用途、是否为编译必需输入。
# 这是已有文件的展示映射，不是跨运行版本索引。
ARTIFACT_FILES = (
    ("paper", "paper-input/others/paper.json", "论文输入", True),
    ("input_manifest", "paper-input/content-list.json", "解析状态", False),
    ("points", "novelty-points.json", "最终提取结果", True),
    ("plans", "retrieval-plans.json", "规划与实际检索", True),
    ("candidate_audit", "candidate-audit.json", "候选审计", False),
    ("references", "references/list.json", "检索文献与材料目录", False),
    ("subject_references", "subject_references/list.json", "原论文参考文献目录", False),
    ("evidence", "evidence-cards.json", "证据构建、Validator 与拒卡", True),
    ("reviews", "novelty-reviews.json", "单卡／查新点评审与 Gate A", False),
    ("report", "report.json", "结论、局部成果、补检与限制", True),
)

MISSING = "未记录；不能据此认定已执行或已通过。"


def _cell(value: Any) -> str:
    if value is None or value == "":
        return "—"
    if isinstance(value, list):
        return "；".join(_cell(item) for item in value) or "—"
    return str(value).replace("|", "\\|").replace("\r", "").replace("\n", "<br>")


def _table(headers, rows) -> str:
    rows = list(rows)
    if not rows:
        return MISSING
    return "\n".join([
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
        *("| " + " | ".join(_cell(cell) for cell in row) + " |" for row in rows),
    ])


def _link(workspace: Path, destination: Path, relative: str) -> str:
    path = workspace / relative
    if not path.resolve().is_relative_to(workspace.resolve()):
        return "路径超出工作目录，未引用"
    if not path.is_file():
        return "文件缺失"
    target = quote(Path(os.path.relpath(path.resolve(), destination.parent.resolve())).as_posix(), safe="/.")
    return f"[查看文件](<{target}>)"


def _review_rows(reviews):
    for review in reviews:
        yield (review.get("novelty_point_id"), review.get("status"),
               review.get("verdict"), review.get("verdict_reason"),
               review.get("incomplete_reason"), len(review.get("read_citations", [])),
               len(review.get("review_evidence", [])))


def build_stage_context(data: Mapping[str, Any], workspace: Path, destination: Path) -> dict[str, str]:
    """只摘要已有业务字段；全文、完整比较和诊断细节通过附件定位。"""
    paper, plans, evidence, report = (data[key] for key in ("paper", "plans", "evidence", "report"))
    inputs, reviews = data["input_manifest"], data["reviews"]
    point_plans = plans.get("novelty_point_plans", [])
    tasks = [task for plan in point_plans for task in plan.get("research_tasks", [])]
    decisions = report.get("recovery_decisions", [])
    supplement = [item for item in decisions
                  if item.get("action") in {"research_gap", "replan_query"}
                  or (item.get("action") == "stop" and item.get("trigger_codes"))]
    local = [item for item in decisions if item not in supplement]

    input_summary = _table(["项目", "落盘记录"], [
        ("论文 ID", paper.get("paper_id")),
        ("输入来源", inputs.get("source", "未记录")),
        ("页数", inputs.get("page_count", "未记录")),
        ("参考文献数量", inputs.get("reference_count", "未记录")),
        ("解析警告", inputs.get("parse_warnings", "未记录")),
        ("全文", _link(workspace, destination, "paper-input/full.md")),
    ])
    extraction = (
        f"最终落盘查新点：{len(data['points'].get('novelty_points', []))} 个。"
        "提取、去重及补充的中间轨迹尚无统一的工作区入口；不能由最终点数推断过程已完成。"
    )
    planning = _table(["查新点", "任务", "类型", "轮次／attempt", "任务说明"], (
        (task.get("novelty_point_id"), task.get("task_id"), task.get("task_type"),
         task.get("attempt"), task.get("description")) for task in tasks
    ))
    candidates = _table(["查新点", "任务", "执行状态", "候选数量"], (
        (task.get("novelty_point_id"), task.get("task_id"), task.get("status"),
         len(task.get("candidates", []))) for task in data["candidate_audit"].get("tasks", [])
    ))
    materials = _table(["材料目录", "Work 数量", "来源记录数量", "材料数量"], (
        (label, len(data[key].get("works", [])), len(data[key].get("source_records", [])),
         len(data[key].get("artifacts", [])))
        for key, label in (("references", "检索文献"), ("subject_references", "原论文参考文献"))
        if data[key]
    )) + "\n\n材料落盘不代表已阅读；读取位置见评审产物中的 read_citations、reader_observations 和任务审计附件。"
    validation = _table(["阶段", "数量／状态"], [
        ("原始构建卡", len(evidence.get("raw_evidence_cards", []))),
        ("Validator 通过卡", len(evidence["validator_accepted_cards"])
         if "validator_accepted_cards" in evidence else "未单独记录"),
        ("最终 accepted 卡", len(evidence.get("accepted_evidence_cards", []))),
        ("拒绝记录", len(evidence.get("rejected_evidence", []))),
    ]) + "\n\n" + _table(["拒绝卡", "原因"], (
        (item.get("card_id"), item.get("reason")) for item in evidence.get("rejected_evidence", [])
    ))
    review_summary = f"评审阶段：{reviews.get('phase', '未记录')}。\n\n" + _table(
        ["查新点", "状态", "裁定", "理由", "未完成原因", "读取引用数", "核验证据数"],
        _review_rows(reviews.get("reviews", [])),
    )
    card_rows = [
        (item.get("card_id"), item.get("status"), (item.get("review") or {}).get("verdict"),
         (item.get("review") or {}).get("verdict_reason"))
        for item in reviews.get("card_reviews", [])
    ]
    card_rows.extend(
        (item.get("card_id"), f"汇总 {item.get('summary_status', '未完成')}（局部成果）",
         item.get("review", {}).get("verdict"), item.get("review", {}).get("verdict_reason"))
        for item in report.get("partial_card_reviews", [])
    )
    review_summary += "\n\n### 单卡及局部评审\n\n" + _table(
        ["卡片", "执行／汇总状态", "局部裁定", "理由"], card_rows
    ) + "\n\n局部评审不能替代查新点最终裁定；完整特征比较及原文核验引用见评审附件。"
    integrity = reviews.get("integrity")
    gates = _table(["Gate", "落盘检查结果"], [
        ("Gate A", "通过" if integrity.get("validation_passed") is True else
         "未通过" if integrity.get("validation_passed") is False else MISSING)
        if isinstance(integrity, dict) else ("Gate A", MISSING),
        ("Gate B", "尚无独立工作区产物；不能由报告已生成推断检查通过。"),
    ])
    failures = _table(["节点", "查新点／任务", "错误码", "原因", "执行状态", "建议动作"], (
        (item.get("scope", {}).get("node"),
         [item.get("scope", {}).get("point_id"), item.get("scope", {}).get("task_id")],
         item.get("code"), item.get("message"), item.get("execution_status"), item.get("recovery"))
        for item in report.get("execution_failures", [])
    ))
    failures += "\n\n### 局部恢复决策\n\n" + _table(["查新点", "动作", "原因"], (
        (item.get("point_id"), item.get("action"), item.get("reason")) for item in local
    )) + "\n\n错误和恢复建议不等于已执行纠错或复核通过；构建补正结果见对应任务审计附件。"
    supplement_summary = _table(["轮次", "查新点", "触发码", "动作", "规划状态", "新增任务", "原因"], (
        (item.get("round_number"), item.get("point_id"), item.get("trigger_codes"),
         item.get("action"), item.get("coordinator_status"), item.get("planned_task_ids"),
         item.get("reason")) for item in supplement
    )) + "\n\n补检规划完成不等于任务执行成功；实际执行情况见检索结果和候选审计。"
    files = [(label, relative, _link(workspace, destination, relative))
             for _, relative, label, _ in ARTIFACT_FILES]
    files.append(("论文全文", "paper-input/full.md", _link(workspace, destination, "paper-input/full.md")))
    # 仅列出本次落盘计划明确引用的任务，不扫描其他任务／历史运行目录。
    seen = set()
    for task in tasks:
        relative = f"research-runs/{task.get('novelty_point_id')}/{task.get('task_id')}/attempt-{task.get('attempt')}.json"
        if relative not in seen:
            seen.add(relative)
            files.append(("任务读取、构建及补正审计", relative, _link(workspace, destination, relative)))
    return {
        "input_status": input_summary, "extraction_status": extraction,
        "planning_summary": planning, "candidate_summary": candidates,
        "material_summary": materials, "validation_summary": validation,
        "review_summary": review_summary, "gate_summary": gates,
        "local_recovery_summary": failures, "supplement_summary": supplement_summary,
        "artifact_directory": _table(["阶段产物", "工作区相对路径", "附件状态／入口"], files),
    }
