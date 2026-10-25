"""展示模板的事实、编号、限制和独立重渲染契约。"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from novelty_agent_framework.tools.renderer import ReportRenderError, render_report
from test_renderer import refresh_fixture_versions, seed_workspace


def render_pair(tmp_path):
    return [render_report(paper_name="renderer-paper", output_root=tmp_path,
                          template_name=name) for name in ("default", "concise")]


def update_report(tmp_path, **fields):
    workspace = tmp_path / "renderer-paper"
    path = workspace / "report.json"
    data = json.loads(path.read_text())
    data.update(fields)
    path.write_text(json.dumps(data))
    refresh_fixture_versions(workspace)


def test_concise_follows_reference_structure_and_keeps_authority(tmp_path):
    seed_workspace(tmp_path)
    complete_path, concise_path = render_pair(tmp_path)
    complete, concise = complete_path.read_text(), concise_path.read_text()
    assert complete_path != concise_path
    assert concise_path.name == "renderer-paper-concise.md"
    for title in ("一、查新目的", "二、项目的科学技术要点", "三、查新点",
                  "四、查新范围要求", "五、文献检索范围及检索策略", "六、检索结果", "七、查新结论"):
        assert f"## {title}" in concise
    for fact in ("Renderer Test \\| Paper", "图学习方法", "Related Paper", "存在部分技术差异。"):
        assert fact in complete and fact in concise
    assert "[1] **Related Paper**" in concise
    assert "**查新点 1**\n\n存在部分技术差异。" in concise
    assert "既有工作覆盖部分特征。" not in concise
    assert "直接覆盖核心特征。" not in concise
    assert "## 八、备注" not in concise
    assert "[原文]" not in concise
    assert "https://arxiv.org/pdf/1234.5678" in concise
    assert concise.count("Related Paper") == 1
    for debug in ("Gate", "Reviewer", "C1", "NP-1", "work-1", "置信度", "{{", "阶段产物", "报告摘要"):
        assert debug not in concise
    assert len(concise) < len(complete)


@pytest.mark.parametrize("reason,label,description", [
    ("semantic_evidence", "证据不足，无法裁定", "现有证据不足以支持判断"),
    ("material_unavailable", "核验未完成，无法裁定", "所需文献材料未能取得"),
    ("technical_error", "核验未完成，无法裁定", "技术问题"),
    ("budget_exhausted", "核验未完成，无法裁定", "资源额度已用尽"),
])
def test_coordinator_summary_takes_priority_even_when_review_incomplete(tmp_path, reason, label, description):
    seed_workspace(tmp_path)
    update_report(tmp_path, conclusions=[{
        "novelty_point_id": "NP-1", "review_status": "insufficient_evidence",
        "incomplete_reason": reason, "summary": "局部有成果，不能当作最终裁定。",
    }], limitations=[f"NP-1：Reviewer 核验未完成；原因：{reason}。", "仅覆盖英文摘要。"],
        missing_references=["Missing paper"], missing_baselines=["Baseline method"], citation_issues=["引文位置未确认"])
    complete, concise = [path.read_text() for path in render_pair(tmp_path)]
    assert label in complete
    assert "局部有成果，不能当作最终裁定。" in concise
    assert description not in concise and reason not in concise
    assert "仅覆盖英文摘要。" in concise
    assert "Missing paper" in concise and "对比方法 method" in concise and "引文位置未确认" in concise


def test_numbered_comparisons_deduplicate_same_work_across_points(tmp_path):
    seed_workspace(tmp_path)
    workspace = tmp_path / "renderer-paper"
    points_path = workspace / "novelty-points.json"
    points = json.loads(points_path.read_text())
    points["novelty_points"].append({"point_id": "NP-2", "claim": "第二查新点"})
    points_path.write_text(json.dumps(points))
    evidence_path = workspace / "evidence-cards.json"
    evidence = json.loads(evidence_path.read_text())
    extra = dict(evidence["accepted_evidence_cards"][0], card_id="C2", novelty_point_id="NP-2")
    evidence["accepted_evidence_cards"].append(extra)
    evidence_path.write_text(json.dumps(evidence))
    report_path = workspace / "report.json"
    report = json.loads(report_path.read_text())
    first = report["conclusions"][0]
    first["summary"] = ""  # Coordinator did not return usable prose: use comparison fallback.
    first["feature_comparisons"] = [{"feature_id": "F1", "work_id": "work-1",
        "relation": "partially_supported", "reason": "已有文献只覆盖静态图，尚未覆盖动态更新。", "evidence_refs": ["E1"]}]
    report["conclusions"].append(dict(first, novelty_point_id="NP-2", highly_relevant_works=[{
        "work_id": "work-1", "card_ids": ["C2"], "relevance_reason": "关联第二查新点。"}]))
    # Updating an upstream dependency requires explicit registration of downstream snapshots.
    from novelty_agent_framework.report_inputs import record_report_input_files
    record_report_input_files(workspace, ("novelty-points.json", "retrieval-plans.json", "evidence-cards.json"))
    update_report(tmp_path, **report)
    concise = render_pair(tmp_path)[1].read_text()
    assert "相关文献 1 篇" in concise and concise.count("[1] **Related Paper**") == 1
    assert concise.count("文献[1]（与该特征部分相符）：已有文献只覆盖静态图") == 2
    assert "查新点 1：部分新颖" in concise and "查新点 2：部分新颖" in concise
    assert "F1" not in concise and "E1" not in concise


def test_no_valid_papers_or_missing_conclusion_is_not_a_novelty_finding(tmp_path):
    seed_workspace(tmp_path)
    workspace = tmp_path / "renderer-paper"
    evidence_path = workspace / "evidence-cards.json"
    evidence = json.loads(evidence_path.read_text())
    evidence["accepted_evidence_cards"] = []
    evidence_path.write_text(json.dumps(evidence))
    refresh_fixture_versions(workspace, "evidence-cards.json")
    update_report(tmp_path, conclusions=[])
    concise = render_pair(tmp_path)[1].read_text()
    assert "不能据此认定相关文献不存在" in concise
    assert "尚未形成结论" in concise
    assert "Related Paper" not in concise


def test_selection_errors_and_changed_snapshot_preserve_both_outputs(tmp_path):
    seed_workspace(tmp_path)
    paths = render_pair(tmp_path)
    previous = [path.read_bytes() for path in paths]
    for kwargs in ({"template_name": "unknown"}, {"template_name": "concise", "template_path": "custom.md"}):
        with pytest.raises(ReportRenderError):
            render_report(paper_name="renderer-paper", output_root=tmp_path, **kwargs)
    material = tmp_path / "renderer-paper/evidence-cards.json"
    material.write_bytes(material.read_bytes() + b"\n")
    for name in ("default", "concise"):
        with pytest.raises(ReportRenderError, match="版本不一致"):
            render_report(paper_name="renderer-paper", output_root=tmp_path, template_name=name)
    assert [path.read_bytes() for path in paths] == previous


def test_concise_renders_in_fresh_process_with_same_materials(tmp_path):
    seed_workspace(tmp_path)
    first = render_pair(tmp_path)[1].read_text()
    index = tmp_path / "renderer-paper/report-inputs.json"
    before = index.read_bytes()
    root = Path(__file__).resolve().parents[1]
    env = dict(os.environ, PYTHONPATH=os.pathsep.join([str(root / "backend/src"), str(root)]))
    destination = tmp_path / "exports/concise.md"
    subprocess.run([sys.executable, "-c", "from novelty_agent_framework.tools.renderer import render_report; "
        "render_report(paper_name='renderer-paper',output_root='.',template_name='concise',save_path='exports/concise.md')"],
        cwd=tmp_path, env=env, check=True, capture_output=True)
    assert [line for line in first.splitlines() if "编制日期" not in line] == [
        line for line in destination.read_text().splitlines() if "编制日期" not in line]
    assert index.read_bytes() == before


def test_technical_diagnostics_are_only_in_default_report(tmp_path):
    seed_workspace(tmp_path)
    update_report(tmp_path, limitations=[
        "NP-1：已记录检索执行：arxiv 成功 2 次、失败 1 次、未执行 0 项。以上仅为已记录的执行范围，不代表完整检索或新颖性裁定。",
        "NP-1 恢复停止：Configured round limit reached; unresolved evidence remains unresolved.",
        "部分单卡核验已保存，但查新点汇总未完成；局部核验不能替代最终裁定。",
        "未检查非英语文献及付费全文。",
        "已达到本次补充检索上限，证据缺口仍未解决。",
        "本次检索或核验曾出现执行问题；错误记录本身不能证明问题已恢复。",
    ], partial_card_reviews=[{"card_id": "C1"}], recovery_decisions=[{
        "action": "stop", "reason": "Configured round limit reached; unresolved evidence remains unresolved."}])
    complete, concise = [path.read_text() for path in render_pair(tmp_path)]
    assert "部分检索未成功完成" not in concise
    assert "未检查非英语文献及付费全文。" in concise
    for diagnostic in ("已达到本次补充检索上限", "执行问题", "Configured", "单卡", "成功 2 次"):
        assert diagnostic in complete and diagnostic not in concise
    assert "完整性检查" not in concise


@pytest.mark.parametrize("summary", [None, "", "  ",
    "Reviewer 核验未完成，尚不能作出新颖性裁定。",
    "该查新点的关键比较证据不足，尚不能作出新颖性裁定。"])
def test_missing_coordinator_prose_uses_existing_review_fallback(tmp_path, summary):
    seed_workspace(tmp_path)
    path = tmp_path / "renderer-paper/report.json"
    report = json.loads(path.read_text())
    report["conclusions"][0]["summary"] = summary
    update_report(tmp_path, **report)
    concise = render_pair(tmp_path)[1].read_text()
    assert "查新点 1：部分新颖" in concise
    assert "文献[1]：直接覆盖核心特征。" in concise
    assert "既有工作覆盖部分特征。" in concise


def test_unavailable_url_keeps_title_metadata_without_link_placeholder(tmp_path):
    seed_workspace(tmp_path)
    workspace = tmp_path / "renderer-paper"
    path = workspace / "evidence-cards.json"
    data = json.loads(path.read_text())
    data["accepted_evidence_cards"][0]["sources"] = []
    path.write_text(json.dumps(data))
    refresh_fixture_versions(workspace, "evidence-cards.json")
    concise = render_pair(tmp_path)[1].read_text()
    assert "[1] **Related Paper**" in concise
    assert "原文链接未记录" not in concise and "[原文]" not in concise


def test_concise_queries_use_planner_strategies_including_unexecuted_levels(tmp_path):
    seed_workspace(tmp_path)
    workspace = tmp_path / 'renderer-paper'
    path = workspace / 'retrieval-plans.json'
    plans = json.loads(path.read_text())
    point_plan = plans['novelty_point_plans'][0]
    point_plan['search_plans'] = [{
        'concepts': [
            {'concept_id':'C1', 'terms':['graph learning'], 'alias':['graph representation'], 'exclude':['survey']},
            {'concept_id':'C10', 'terms':['dynamic graph', 'temporal graph'], 'alias':[], 'exclude':[]},
        ],
        'strategies': [
            {'expression':'C1 AND C10', 'level':'strict', 'use_alias':False, 'use_exclude':True},
            {'expression':'C1 AND C10', 'level':'medium', 'use_alias':True, 'use_exclude':False},
            {'expression':'C10', 'level':'broad', 'use_alias':True, 'use_exclude':False},
        ],
    }]
    # Repeated rounds should not duplicate identical planned expressions.
    point_plan['search_plans'] *= 2
    point_plan['query_plan']['queries'] = ['ti:EXECUTED_COPY']
    path.write_text(json.dumps(plans))
    refresh_fixture_versions(workspace, 'retrieval-plans.json', 'evidence-cards.json')
    complete, concise = [p.read_text() for p in render_pair(tmp_path)]
    section = concise.split('### （三）检索式\n\n')[1].split('\n\n## 六、')[0]
    assert section.splitlines() == [
        '1. ("graph learning" AND NOT ("survey")) AND ("dynamic graph" OR "temporal graph")',
        '2. ("graph learning" OR "graph representation") AND ("dynamic graph" OR "temporal graph")',
        '3. ("dynamic graph" OR "temporal graph")',
    ]
    assert 'all:"graph learning"' in complete
    assert 'all:' not in section and 'ti:' not in section and 'C10' not in section


def test_missing_planner_strategies_do_not_fall_back_to_execution_queries(tmp_path):
    seed_workspace(tmp_path)
    workspace = tmp_path / 'renderer-paper'
    path = workspace / 'retrieval-plans.json'
    plans = json.loads(path.read_text())
    plans['novelty_point_plans'][0]['search_plans'] = []
    path.write_text(json.dumps(plans))
    refresh_fixture_versions(workspace, 'retrieval-plans.json', 'evidence-cards.json')
    complete, concise = [p.read_text() for p in render_pair(tmp_path)]
    assert 'all:"graph learning"' in complete
    assert 'all:"graph learning"' not in concise
    assert '未记录计划检索式。' in concise
