"""确定性 Markdown 报告 Renderer 测试。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from novelty_agent_framework.persistence import (
    persist_evidence_cards,
    persist_novelty_points,
    persist_report,
    persist_retrieval_plans,
    persist_workflow_input,
)
from novelty_agent_framework.schemas import (
    EvidenceCard,
    EvidenceSource,
    NoveltyConclusion,
    NoveltyPoint,
    NoveltyReport,
    NoveltyVerdict,
    PaperInput,
    RelevantWork,
    ResearchTask,
    SearchConcept,
    SearchPlan,
    ReviewStatus,
    SearchStrategy,
)
from novelty_agent_framework.tools.renderer import (
    ReportRenderError,
    _format_conclusions,
    render_report,
)


def seed_workspace(output_root: str | Path = "outputs") -> None:
    paper = PaperInput(
        paper_id="renderer-paper",
        title="Renderer Test | Paper",
        abstract="Test abstract.",
        full_text="# Test\n\nBody",
        keywords_en=["graph learning"],
    )
    point = NoveltyPoint(
        point_id="NP-1",
        claim="图学习方法",
        claim_en="Graph learning method",
    )
    task = ResearchTask(
        task_id="T1",
        novelty_point_id="NP-1",
        task_type="literature_search",
        language="en",
        description="graph learning",
    )
    plan = SearchPlan(
        task_id="T1",
        novelty_point_id="NP-1",
        concepts=[
            SearchConcept(concept_id="C1", name="graph learning", terms=["graph learning"])
        ],
        strategies=[SearchStrategy(strategy_id="S1", level="strict", expression="C1")],
    )
    card = EvidenceCard(
        card_id="C1",
        task_id="T1",
        novelty_point_id="NP-1",
        document_title="Related Paper",
        main_contribution="Related contribution",
        sources=[
            EvidenceSource(
                title="arXiv",
                url="https://arxiv.org/abs/1234.5678",
                quote="Exact source text.",
                location="artifact art_x chars:0-18",
            )
        ],
        relevance=0.9,
        confidence=0.8,
    )
    report = NoveltyReport(
        paper_id=paper.paper_id,
        conclusions=[
            NoveltyConclusion(
                novelty_point_id="NP-1",
                review_status=ReviewStatus.REVIEWED,
                verdict=NoveltyVerdict.PARTIALLY_NOVEL,
                verdict_reason="既有工作覆盖部分特征。",
                summary="存在部分技术差异。",
                supporting_card_ids=["C1"],
                confidence=0.8,
                highly_relevant_works=[
                    RelevantWork(
                        work_id="work-1",
                        card_ids=["C1"],
                        evidence_ids=["E1"],
                        relevance_reason="直接覆盖核心特征。",
                    )
                ],
            )
        ],
    )
    persist_workflow_input(paper, output_root=output_root)
    persist_novelty_points(paper, [point], output_root=output_root)
    persist_retrieval_plans(
        paper,
        [task],
        search_plans=[plan],
        executed_queries=[
            {
                "database": "arxiv",
                "novelty_point_id": "NP-1",
                "task_id": "T1",
                "strategy_id": "S1",
                "level": "strict",
                "query": 'all:"graph learning"',
            }
        ],
        rounds=1,
        point_order=["NP-1"],
        output_root=output_root,
    )
    persist_evidence_cards(
        paper,
        raw_cards=[card],
        accepted_cards=[card],
        rejected_evidence=[],
        output_root=output_root,
    )
    persist_report(paper, report, output_root=output_root)


def test_markdown_renderer_reads_workspace_and_uses_default_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    seed_workspace()

    path = render_report(paper_name="renderer-paper")
    content = path.read_text(encoding="utf-8")

    assert path == Path("outputs/renderer-paper/report/renderer-paper-report.md")
    assert "# 科技查新报告" in content
    assert "Renderer Test \\| Paper" in content
    assert "Graph learning method" in content
    assert "Related Paper" in content
    assert "引文：Exact source text." in content
    assert "位置：artifact art_x chars:0-18" in content
    assert "存在部分技术差异" in content
    assert "Reviewer 裁定：** 部分新颖" in content
    assert "work-1：直接覆盖核心特征" in content
    assert "最终有效证据数量" in content
    assert "| NP-1 | 1 | 有证据 |" in content
    assert "{{" not in content


def test_attachments_list_retrieved_candidates_and_card_sources(tmp_path: Path) -> None:
    seed_workspace(tmp_path)
    workspace = tmp_path / "renderer-paper"
    references = workspace / "references" / "list.json"
    references.parent.mkdir(parents=True, exist_ok=True)
    references.write_text(json.dumps({"source_records": [
        {"title": "AliGraph: A Comprehensive Graph Neural Network Platform",
         "landing_url": "https://arxiv.org/abs/1902.08730",
         "full_text_url": "https://arxiv.org/pdf/1902.08730"},
        {"title": "AliGraph duplicate", "landing_url": "http://arxiv.org/abs/1902.08730"},
        {"title": "Candidate without an evidence card", "landing_url": "https://doi.org/10.1/example"},
        {"title": "Local artifact", "landing_url": "file:///tmp/private.txt"},
    ]}), encoding="utf-8")
    content = render_report(paper_name="renderer-paper", output_root=tmp_path).read_text()
    attachments = content.split("### 检索到的文献", 1)[1]
    expected = "AliGraph: A Comprehensive Graph Neural Network Platform：[https://arxiv.org/pdf/1902.08730](https://arxiv.org/pdf/1902.08730)"
    assert expected in attachments
    assert "AliGraph duplicate" not in attachments
    assert "Candidate without an evidence card：[https://doi.org/10.1/example](https://doi.org/10.1/example)" in attachments
    assert "Related Paper：[https://arxiv.org/pdf/1234.5678](https://arxiv.org/pdf/1234.5678)" in attachments
    assert "file:///" not in attachments


@pytest.mark.parametrize(
    ("status", "verdict", "expected"),
    [
        ("reviewed", "novel", "新颖"),
        ("reviewed", "partially_novel", "部分新颖"),
        ("reviewed", "not_novel", "不新颖"),
        ("insufficient_evidence", None, "证据不足，无法裁定"),
    ],
)
def test_renderer_uses_authoritative_report_verdict(status, verdict, expected):
    rendered = _format_conclusions(
        [
            {
                "novelty_point_id": "NP-1",
                "review_status": status,
                "verdict": verdict,
                "verdict_reason": None if verdict is None else "review reason",
                "confidence": None if verdict is None else 0.75,
                "summary": "report summary",
                "highly_relevant_works": [],
            }
        ],
        {"NP-1": {"claim": "claim"}},
    )

    assert f"Reviewer 裁定：** {expected}" in rendered
    if verdict == "not_novel":
        assert "弱创新" not in rendered


def test_renderer_rejects_unsupported_format() -> None:
    with pytest.raises(ReportRenderError, match="不支持的报告格式"):
        render_report(output_format="pdf", paper_name="renderer-paper")


def test_renderer_requires_all_workspace_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "outputs" / "incomplete").mkdir(parents=True)

    with pytest.raises(ReportRenderError, match="缺少报告输入产物"):
        render_report(paper_name="incomplete")


def test_custom_template_and_save_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    seed_workspace()
    template = tmp_path / "custom.md"
    template.write_text("# {{ paper_name }}\n\n{{ novelty_conclusions }}", encoding="utf-8")

    path = render_report(
        output_format="md",
        template_path=template,
        paper_name="renderer-paper",
        save_path=tmp_path / "custom-report",
    )

    assert path == tmp_path / "custom-report.md"
    assert json.loads(
        (tmp_path / "outputs/renderer-paper/report.json").read_text(encoding="utf-8")
    )["paper_id"] == "renderer-paper"


def test_renderer_reads_only_the_requested_output_root(tmp_path: Path) -> None:
    run_root = tmp_path / "runs" / "0002"
    seed_workspace(run_root)

    path = render_report(paper_name="renderer-paper", output_root=run_root)

    assert path == run_root / "renderer-paper/report/renderer-paper-report.md"
    assert path.is_file()
    assert not (tmp_path / "outputs" / "renderer-paper").exists()


def test_web_material_is_never_listed_and_advice_requires_no_papers(tmp_path):
    seed_workspace(tmp_path)
    workspace = tmp_path / 'renderer-paper'
    path = workspace / 'references/list.json'
    path.parent.mkdir(exist_ok=True)
    web = {'source_kind': 'web_supplement', 'title': 'WEB TITLE MUST NOT APPEAR',
           'landing_url': 'https://web.example/advice',
           'provenance': {'query': 'graph partitioning'}}
    path.write_text(json.dumps({'source_records': [web]}))
    # A paper card already exists: no supplementary advice or Web bibliography.
    content = render_report(paper_name='renderer-paper', output_root=tmp_path).read_text()
    assert web['title'] not in content and web['landing_url'] not in content
    assert '无论文可用时的补充信息建议' not in content
    evidence_path = workspace / 'evidence-cards.json'
    evidence = json.loads(evidence_path.read_text())
    evidence['raw_evidence_cards'] = evidence['accepted_evidence_cards'] = []
    evidence_path.write_text(json.dumps(evidence))
    content = render_report(paper_name='renderer-paper', output_root=tmp_path).read_text()
    assert '无论文可用时的补充信息建议' in content
    assert 'graph partitioning' in content
    assert web['title'] not in content and web['landing_url'] not in content
    # Retrieved paper without a card still prevents a false no-paper recommendation.
    path.write_text(json.dumps({'source_records': [web, {
        'source_kind': 'structured_database', 'title': 'Unread paper',
        'landing_url': 'https://doi.org/10.1/unread'}]}))
    content = render_report(paper_name='renderer-paper', output_root=tmp_path).read_text()
    assert '无论文可用时的补充信息建议' not in content
    assert 'Unread paper' in content


def test_legacy_web_card_is_not_rendered_as_related_literature():
    from novelty_agent_framework.tools.renderer import _format_retrieved_references, _is_web_card
    manifest = {'source_records': [{'source_kind': 'web', 'title': 'Web source',
                                    'landing_url': 'https://web.example/a'}]}
    card = {'document_title': 'Old Web card', 'sources': [{'url': 'https://web.example/a'}]}
    assert _is_web_card(card, manifest)
    assert 'Old Web card' not in _format_retrieved_references(manifest, [card])
    assert 'Web source' not in _format_retrieved_references(manifest, [card])


def test_default_report_displays_stage_results_and_keeps_recovery_separate(tmp_path):
    seed_workspace(tmp_path)
    workspace = tmp_path / "renderer-paper"
    (workspace / "novelty-reviews.json").write_text(json.dumps({
        "paper_id": "renderer-paper", "phase": "complete",
        "integrity": {"validation_passed": False},
        "reviews": [{"novelty_point_id": "NP-1", "status": "reviewed",
                     "verdict": "partially_novel", "verdict_reason": "正式评审理由"}],
        "card_reviews": [{"card_id": "C1", "status": "completed",
                          "review": {"verdict_reason": "单卡核验理由"}}],
    }), encoding="utf-8")
    path = workspace / "report.json"
    report = json.loads(path.read_text())
    report["execution_failures"] = [{"scope": {"node": "reader", "task_id": "T1"},
        "code": "material.unavailable", "message": "原文未取得", "recovery": "fetch_fulltext"}]
    report["recovery_decisions"] = [
        {"point_id": "NP-1", "action": "read_artifact", "reason": "局部补读"},
        {"point_id": "NP-1", "action": "stop", "reason": "技术失败停止"},
        {"point_id": "NP-1", "action": "research_gap", "reason": "追加研究任务",
         "round_number": 2, "coordinator_status": "planned", "planned_task_ids": ["T2"]},
    ]
    path.write_text(json.dumps(report), encoding="utf-8")
    content = render_report(paper_name="renderer-paper", output_root=tmp_path).read_text()
    assert "正式评审理由" in content and "单卡核验理由" in content
    assert "| Gate A | 未通过 |" in content
    assert "Gate B | 尚无独立工作区产物" in content
    local, supplement = content.split("## 八、现场问题与局部恢复")[1].split("## 九、任务级补检")
    assert "局部补读" in local and "原文未取得" in local
    assert "技术失败停止" in local and "技术失败停止" not in supplement
    assert "追加研究任务" not in local
    assert "追加研究任务" in supplement and "局部补读" not in supplement
    assert "全文及详细审计保留在附件中" in content


def test_render_does_not_create_missing_inputs_and_links_follow_save_path(tmp_path):
    seed_workspace(tmp_path)
    workspace = tmp_path / "renderer-paper"
    references = workspace / "references/list.json"
    references.unlink()
    original = {p.relative_to(workspace): p.read_bytes() for p in workspace.rglob("*") if p.is_file()}
    path = render_report(paper_name="renderer-paper", output_root=tmp_path,
                         save_path=tmp_path / "export dir" / "report")
    content = path.read_text()
    assert "| 检索文献与材料目录 | references/list.json | 文件缺失 |" in content
    assert "(<../renderer-paper/paper-input/others/paper.json>)" in content
    assert not references.exists()
    assert original == {p.relative_to(workspace): p.read_bytes() for p in workspace.rglob("*") if p.is_file()}


@pytest.mark.parametrize("payload, message", [
    ("{", "读取报告输入产物失败"),
    ("[]", "应为 JSON 对象"),
    ('{"paper_id": "another-paper"}', "论文身份不一致"),
])
def test_invalid_optional_artifact_fails_explicitly(tmp_path, payload, message):
    seed_workspace(tmp_path)
    (tmp_path / "renderer-paper/novelty-reviews.json").write_text(payload)
    with pytest.raises(ReportRenderError, match=message):
        render_report(paper_name="renderer-paper", output_root=tmp_path)


def test_same_snapshot_renders_in_fresh_process(tmp_path):
    import os
    import subprocess
    import sys

    seed_workspace(tmp_path)
    first = render_report(paper_name="renderer-paper", output_root=tmp_path).read_text()
    root = Path(__file__).resolve().parents[1]
    env = dict(os.environ, PYTHONPATH=os.pathsep.join([str(root / "backend/src"), str(root)]))
    subprocess.run([sys.executable, "-c",
        "from novelty_agent_framework.tools.renderer import render_report; "
        "render_report(paper_name='renderer-paper', output_root='.')"],
        cwd=tmp_path, env=env, check=True, capture_output=True, text=True)
    second = (tmp_path / "renderer-paper/report/renderer-paper-report.md").read_text()
    assert [line for line in first.splitlines() if "报告生成时间" not in line] == [
        line for line in second.splitlines() if "报告生成时间" not in line]
