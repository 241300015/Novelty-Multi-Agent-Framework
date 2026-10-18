"""编译入口阻断混用／过期输入；报告时间与材料时间独立。"""

import json
from datetime import datetime, timezone

import pytest

from novelty_agent_framework.persistence import persist_workflow_input
from novelty_agent_framework.report_inputs import (
    INDEX_NAME, ReportInputVersionError, finalize_report_inputs, write_report_artifact,
)
from novelty_agent_framework.schemas import PaperInput
from novelty_agent_framework.tools import renderer
from novelty_agent_framework.tools.renderer import ReportRenderError, render_report
from test_renderer import seed_workspace


@pytest.mark.parametrize("relative", [
    "paper-input/others/paper.json", "paper-input/full.md", "novelty-points.json",
    "retrieval-plans.json", "evidence-cards.json", "references/list.json", "report.json",
])
def test_changed_material_cannot_replace_existing_report(tmp_path, relative):
    seed_workspace(tmp_path)
    workspace = tmp_path / "renderer-paper"
    destination = render_report(paper_name="renderer-paper", output_root=tmp_path)
    previous = destination.read_bytes()
    path = workspace / relative
    path.write_bytes(path.read_bytes() + b"\n")

    with pytest.raises(ReportRenderError, match="版本不一致"):
        render_report(paper_name="renderer-paper", output_root=tmp_path)
    assert destination.read_bytes() == previous


@pytest.mark.parametrize("damage, message", [
    ("no_index", "缺少报告版本记录"),
    ("schema", "格式或版本不受支持"),
    ("foreign_run", "运行 ID 不一致"),
    ("collecting", "尚未封存"),
    ("bad_time", "材料所属时间无效"),
    ("missing_input", "缺少本次运行的报告输入版本"),
    ("missing_dependency", "依赖版本记录不完整"),
])
def test_version_gate_fails_closed(tmp_path, damage, message):
    seed_workspace(tmp_path)
    workspace = tmp_path / "renderer-paper"
    path = workspace / INDEX_NAME
    index = json.loads(path.read_text())
    if damage == "no_index":
        path.unlink()
    else:
        if damage == "schema":
            index["schema_version"] = 99
        elif damage == "foreign_run":
            index["artifacts"]["evidence-cards.json"]["run_id"] = "other-run"
        elif damage == "collecting":
            index["status"] = "collecting"
        elif damage == "bad_time":
            index["started_at"] = "2999-01-01T00:00:00+00:00"
        elif damage == "missing_dependency":
            index["artifacts"]["report.json"]["dependencies"] = {}
        else:
            del index["artifacts"]["evidence-cards.json"]
        path.write_text(json.dumps(index))
    with pytest.raises(ReportRenderError, match=message):
        render_report(paper_name="renderer-paper", output_root=tmp_path)
    assert not (workspace / "report/renderer-paper-report.md").exists()


def test_new_run_does_not_adopt_old_optional_review(tmp_path):
    seed_workspace(tmp_path)
    workspace = tmp_path / "renderer-paper"
    write_report_artifact(workspace, workspace / "novelty-reviews.json", {"reviews": []})
    seed_workspace(tmp_path)  # Same paper and directory, new run; no new reviews written.
    with pytest.raises(ReportRenderError, match="旧运行产物.*novelty-reviews"):
        render_report(paper_name="renderer-paper", output_root=tmp_path)


def test_review_must_be_refreshed_after_evidence_changes(tmp_path):
    seed_workspace(tmp_path)
    workspace = tmp_path / "renderer-paper"
    review_path = workspace / "novelty-reviews.json"
    write_report_artifact(workspace, review_path, {"reviews": []})
    evidence_path = workspace / "evidence-cards.json"
    evidence = json.loads(evidence_path.read_text())
    evidence["raw_evidence_cards"][0]["main_contribution"] = "Updated contribution"
    write_report_artifact(workspace, evidence_path, evidence)
    report_path = workspace / "report.json"
    write_report_artifact(workspace, report_path, json.loads(report_path.read_text()))
    finalize_report_inputs(workspace)

    with pytest.raises(ReportRenderError, match="依赖版本已变化.*novelty-reviews"):
        render_report(paper_name="renderer-paper", output_root=tmp_path)


def test_late_writer_and_renderer_cannot_use_another_run(tmp_path):
    seed_workspace(tmp_path)
    workspace = tmp_path / "renderer-paper"
    old_run = json.loads((workspace / INDEX_NAME).read_text())["run_id"]
    seed_workspace(tmp_path)
    report_path = workspace / "report.json"
    original = report_path.read_bytes()
    with pytest.raises(ReportInputVersionError, match="运行 ID 不一致"):
        write_report_artifact(workspace, report_path, {}, run_id=old_run)
    assert report_path.read_bytes() == original
    with pytest.raises(ReportRenderError, match="调用方要求的运行 ID 不一致"):
        render_report(paper_name="renderer-paper", output_root=tmp_path, expected_run_id=old_run)


def test_rerender_changes_generation_time_only(tmp_path, monkeypatch):
    seed_workspace(tmp_path)
    workspace = tmp_path / "renderer-paper"
    version_bytes = (workspace / INDEX_NAME).read_bytes()

    class Clock(datetime):
        year_value = 2030

        @classmethod
        def now(cls, tz=None):
            return datetime(cls.year_value, 1, 1, tzinfo=timezone.utc)

    monkeypatch.setattr(renderer, "datetime", Clock)
    first = render_report(paper_name="renderer-paper", output_root=tmp_path).read_text()
    Clock.year_value = 2031
    second = render_report(paper_name="renderer-paper", output_root=tmp_path).read_text()
    assert "报告生成时间 | 2030" in first
    assert "报告生成时间 | 2031" in second
    assert "| 材料所属时间 |" in first
    assert [line for line in first.splitlines() if not line.startswith("| 报告生成时间")] == [
        line for line in second.splitlines() if not line.startswith("| 报告生成时间")]
    assert (workspace / INDEX_NAME).read_bytes() == version_bytes


def test_new_input_replaces_old_full_text_and_blocks_incomplete_run(tmp_path):
    seed_workspace(tmp_path)
    workspace = tmp_path / "renderer-paper"
    persist_workflow_input(PaperInput(paper_id="renderer-paper", title="Changed paper",
                                     full_text="New full text"), output_root=tmp_path)
    assert (workspace / "paper-input/full.md").read_text() == "New full text\n"
    assert json.loads((workspace / "paper-input/content-list.json").read_text())["title"] == "Changed paper"
    with pytest.raises(ReportRenderError, match="尚未封存"):
        render_report(paper_name="renderer-paper", output_root=tmp_path)


def test_unregistered_task_attachment_is_not_linked(tmp_path):
    seed_workspace(tmp_path)
    workspace = tmp_path / "renderer-paper"
    stale = workspace / "research-runs/NP-1/T1/attempt-1.json"
    stale.parent.mkdir(parents=True)
    stale.write_text('{"task_id": "T1", "status": "old"}')
    content = render_report(paper_name="renderer-paper", output_root=tmp_path).read_text()
    assert "research-runs/NP-1/T1/attempt-1.json | 本次运行未登记" in content
    assert "(<../research-runs/NP-1/T1/attempt-1.json>)" not in content
