"""Multi-point/multi-paper fixtures also used for the reviewable rendered example."""
from copy import deepcopy
import json

import pytest

from novelty_agent_framework.report_inputs import record_report_input_files
from novelty_agent_framework.tools.renderer import _build_concise_context, _load_workspace_data
from test_renderer import seed_workspace, refresh_fixture_versions
from test_renderer_concise import render_pair


def seed_multi_workspace(root):
    seed_workspace(root)
    workspace = root / "renderer-paper"
    points = [{"point_id": "NP-1", "claim": "动态图增量表示",
               "technical_features": ["增量更新", "保留历史结构"]},
              {"point_id": "NP-2", "claim": "低资源迁移",
               "technical_features": ["少样本适配", "跨域评估"]}]
    cards, conclusions, reviews = [], [], []
    template = json.loads((workspace / "evidence-cards.json").read_text())["accepted_evidence_cards"][0]
    for i, point in enumerate(points, 1):
        works, comparisons = [], []
        for j in (1, 2):
            cid, wid, eid = f"C{i}{j}", f"work-{i}{j}", f"E{i}{j}"
            card = dict(template, card_id=cid, novelty_point_id=point["point_id"],
                        document_title=f"Paper {i}{j}", evidence_ids=[eid],
                        main_contribution=("研究按时间更新图表示。" if i == 1 else "研究少样本模型适配。"),
                        sources=[{"url": f"https://example.org/paper-{i}{j}"}])
            cards.append(card)
            work = {"work_id": wid, "card_ids": [cid], "evidence_ids": [eid],
                    "relevance_reason": "讨论了该查新点的部分技术环节。"}
            comp = {"feature_id": "F1", "work_id": wid,
                    "relation": "partially_supported" if j == 1 else "unknown",
                    "basis_type": "direct_statement" if j == 1 else "insufficient",
                    "evidence_refs": [eid] if j == 1 else [], "source_context": "author_method",
                    "reason": "材料支持部分实现步骤，适用范围仍有限。" if j == 1 else "已读摘要未说明该机制。"}
            works.append(work)
            comparisons.append(comp)
            reviews.append({"card_id": cid, "novelty_point_id": point["point_id"], "status": "completed",
                "review": {"kind": "card_support", "novelty_point_id": point["point_id"], "status": "reviewed",
                           "support_reason": "摘要可用于有限比较，正文机制仍需核验。",
                           "highly_relevant_works": [work], "feature_comparisons": [comp]}})
        conclusions.append({"novelty_point_id": point["point_id"],
            "review_status": "reviewed" if i == 1 else "insufficient_evidence",
            "verdict": "partially_novel" if i == 1 else None,
            "verdict_reason": "已有工作部分覆盖增量更新，证据支持范围有限。" if i == 1 else None,
            "summary": "已取得的相关文献涉及动态图表示，部分更新步骤存在对应；结论限于已核验材料。" if i == 1 else "",
            "incomplete_reason": None if i == 1 else "semantic_evidence", "confidence": .8 if i == 1 else None,
            "highly_relevant_works": works, "feature_comparisons": comparisons})
    # One missing single-card review and one unresolved identity are intentional.
    reviews.pop()
    conclusions[1]["feature_comparisons"].append({"feature_id": "F2", "work_id": "work-unlinked",
        "relation": "unknown", "reason": "比较对象身份记录不完整。", "evidence_refs": []})
    values = {
        "novelty-points.json": {"paper_id": "renderer-paper", "novelty_points": points},
        "evidence-cards.json": {"paper_id": "renderer-paper", "accepted_evidence_cards": cards},
        "novelty-reviews.json": {"paper_id": "renderer-paper", "card_reviews": reviews},
        "report.json": {"paper_id": "renderer-paper", "conclusions": conclusions,
                        "limitations": ["目前仅取得摘要和局部正文，未覆盖非英语文献。"]},
    }
    for name, value in values.items():
        (workspace / name).write_text(json.dumps(value, ensure_ascii=False))
    record_report_input_files(workspace, ("novelty-points.json", "retrieval-plans.json",
                                         "evidence-cards.json", "novelty-reviews.json"))
    refresh_fixture_versions(workspace)
    return workspace


def context(data):
    return _build_concise_context(data, data["evidence"]["accepted_evidence_cards"])


def test_two_layers_include_multiple_papers_even_with_summary(tmp_path):
    workspace = seed_multi_workspace(tmp_path)
    before = {path.name: path.read_bytes() for path in workspace.glob("*.json")}
    complete, concise = [p.read_text() for p in render_pair(tmp_path)]
    overview, detail = concise.split("### （一）整体查新总结")[1].split("### （二）逐点、逐篇评判")
    assert "2 个查新点" in overview and "4 篇" in overview
    assert "已取得的相关文献涉及动态图表示" in overview
    assert "查新点 1：部分新颖" in overview and "查新点 2：比较证据不足" in overview
    assert "未覆盖非英语文献" in overview
    first, second = detail.split("#### 查新点 2")
    for n in (1, 2):
        assert f"**文献[{n}]**" in first
    for n in (3, 4):
        assert f"**文献[{n}]**" in second
    assert "材料评审：摘要可用于有限比较" in detail
    assert "增量更新：与该特征部分相符" in first
    assert "保留历史结构" in first and "跨域评估" in second
    assert "未读到或未提及不能作为已证实差异" in detail
    assert "未提供已完成的单篇评审" in second
    assert "work-unlinked" in detail and "关联缺失或不唯一" in detail
    assert "文献[5]" not in concise
    assert "整体查新总结" not in complete
    assert before == {path.name: path.read_bytes() for path in workspace.glob("*.json")}


@pytest.mark.parametrize("summary", [None, "", "  ", "历史综述保持不变。"])
def test_legacy_missing_fields_and_summary_do_not_hide_papers(tmp_path, summary):
    data = _load_workspace_data(seed_multi_workspace(tmp_path))
    data.pop("reviews")
    for conclusion in data["report"]["conclusions"]:
        conclusion["summary"] = summary
        conclusion.pop("feature_comparisons")
    result = context(data)
    assert result["concise_conclusions"].count("**文献[") == 4
    assert "未提供已完成的单篇评审" in result["concise_conclusions"]
    assert "尚不能确认" in result["concise_conclusions"]
    if summary and summary.strip():
        assert summary in result["concise_overview"]


def test_unlinked_or_ambiguous_work_is_not_guessed_by_title(tmp_path):
    data = _load_workspace_data(seed_multi_workspace(tmp_path))
    data.pop("reviews")
    conclusion = data["report"]["conclusions"][0]
    conclusion["highly_relevant_works"] = []
    cards = data["evidence"]["accepted_evidence_cards"]
    cards[1]["document_title"] = cards[0]["document_title"]
    before = deepcopy(data)
    detail = context(data)["concise_conclusions"].split("#### 查新点 2")[0]
    assert "work-11" in detail and "work-12" in detail
    assert "未关联展示编号" in detail
    assert data == before
    conclusion["highly_relevant_works"] = [{"work_id": "work-11", "card_ids": ["C11", "C12"]},
        {"work_id": "work-12", "card_ids": ["C12"]}]
    detail = context(data)["concise_conclusions"]
    assert "未关联展示编号的文献（work_id: work-11）" in detail


def test_dangling_evidence_and_missing_card_review_do_not_become_confirmed_findings(tmp_path):
    data = _load_workspace_data(seed_multi_workspace(tmp_path))
    data.pop("reviews")
    data["report"]["conclusions"][0]["feature_comparisons"][0]["evidence_refs"] = ["absent"]
    detail = context(data)["concise_conclusions"]
    assert "证据引用缺失或无法核对，不能确认该判断" in detail
    assert "原比较记录为“与该特征部分相符”" in detail
    assert "未提供已完成的单篇评审" in detail


def test_partial_card_results_survive_failed_point_summary_without_adjudication(tmp_path):
    data = _load_workspace_data(seed_multi_workspace(tmp_path))
    row = data.pop("reviews")["card_reviews"][0]
    row.pop("status")
    row["summary_status"] = "failed"
    # Historical point-shaped card result must never supply the final verdict.
    row["review"].pop("kind")
    row["review"]["verdict"] = "novel"
    data["report"]["partial_card_reviews"] = [row]
    data["report"]["conclusions"] = []
    result = context(data)
    assert "材料评审：摘要可用于有限比较" in result["concise_conclusions"]
    assert "尚未形成结论" in result["concise_overview"]
    assert "：新颖" not in result["concise_overview"]


def test_wrong_point_or_failed_card_rows_are_not_used(tmp_path):
    data = _load_workspace_data(seed_multi_workspace(tmp_path))
    for row in data["reviews"]["card_reviews"]:
        row["review"]["support_reason"] = "不得展示的局部记录"
    data["reviews"]["card_reviews"][0]["status"] = "failed"
    data["reviews"]["card_reviews"][1]["novelty_point_id"] = "NP-2"
    data["reviews"]["card_reviews"][2]["review"]["novelty_point_id"] = "NP-1"
    assert "不得展示的局部记录" not in context(data)["concise_conclusions"]
