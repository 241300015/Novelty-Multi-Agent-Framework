"""Reviewer authoritative fields are deterministically bound into reports."""

from __future__ import annotations

import asyncio

import pytest

from novelty_agent_framework.agents import DemoCoordinator
from novelty_agent_framework.core import bind_reviews_to_report
from novelty_agent_framework.schemas import (
    NoveltyBrief,
    NoveltyConclusion,
    NoveltyPoint,
    NoveltyPointReview,
    NoveltyReport,
    NoveltyVerdict,
    PaperInput,
    RelevantWork,
    ReviewStatus,
)
from novelty_agent_framework.workflows import NoveltyWorkflow, NoveltyWorkflowServices


def _point(point_id: str = "NP-1") -> NoveltyPoint:
    return NoveltyPoint(point_id=point_id, claim="claim")


def _work() -> RelevantWork:
    return RelevantWork(
        work_id="work-1",
        card_ids=["card-1"],
        evidence_ids=["evidence-1"],
        relevance_reason="direct overlap",
    )


def _review(point_id: str = "NP-1") -> NoveltyPointReview:
    return NoveltyPointReview(
        novelty_point_id=point_id,
        status=ReviewStatus.REVIEWED,
        verdict=NoveltyVerdict.NOVEL,
        verdict_reason="authoritative reason",
        confidence=0.83,
        highly_relevant_works=[_work()],
    )


def _drifted_report() -> NoveltyReport:
    return NoveltyReport(
        paper_id="paper-1",
        conclusions=[
            NoveltyConclusion(
                novelty_point_id="NP-1",
                review_status=ReviewStatus.REVIEWED,
                verdict=NoveltyVerdict.NOT_NOVEL,
                verdict_reason="coordinator drift",
                confidence=0.12,
                summary="Coordinator narrative is retained.",
            )
        ],
    )


def test_binding_overwrites_verdict_confidence_reason_and_relevant_works():
    review = _review()
    report = bind_reviews_to_report(
        _drifted_report(),
        novelty_reviews=[review],
        novelty_points=[_point()],
        evidence_cards=[],
    )

    conclusion = report.conclusions[0]
    assert conclusion.summary == "Coordinator narrative is retained."
    assert conclusion.review_status == review.status
    assert conclusion.verdict == review.verdict
    assert conclusion.verdict_reason == review.verdict_reason
    assert conclusion.confidence == review.confidence
    assert conclusion.highly_relevant_works == review.highly_relevant_works


class DriftingCoordinator(DemoCoordinator):
    def synthesize(self, *args, **kwargs):
        report = super().synthesize(*args, **kwargs)
        drifted = report.conclusions[0].model_copy(
            update={
                "verdict": NoveltyVerdict.NOT_NOVEL,
                "verdict_reason": "coordinator drift",
                "confidence": 0.01,
                "highly_relevant_works": [],
            }
        )
        return report.model_copy(update={"conclusions": [drifted]})


def test_workflow_overwrites_fake_coordinator_judgment_drift():
    point = _point()
    review = _review()
    default = NoveltyWorkflow.default()
    workflow = NoveltyWorkflow(
        NoveltyWorkflowServices(
            coordinator=DriftingCoordinator(),
            task_researcher=default.services.task_researcher,
            search_planner=default.services.search_planner,
        )
    )
    output = asyncio.run(
        workflow._synthesize_report(
            {
                "paper": PaperInput(
                    paper_id="paper-1", title="Paper", full_text="body"
                ),
                "novelty_points": [point],
                "brief": NoveltyBrief(
                    paper_summary="summary",
                    novelty_points=[point],
                    research_tasks=[],
                ),
                "evidence_cards": [],
                "novelty_reviews": [review],
                "rejected_evidence": [],
                "insufficient_final_evidence_points": [],
            }
        )
    )

    conclusion = output["report"].conclusions[0]
    assert conclusion.verdict is NoveltyVerdict.NOVEL
    assert conclusion.verdict_reason == "authoritative reason"
    assert conclusion.confidence == 0.83
    assert conclusion.highly_relevant_works == [_work()]


def test_binding_preserves_insufficient_evidence_without_verdict():
    review = NoveltyPointReview(
        novelty_point_id="NP-1",
        status=ReviewStatus.INSUFFICIENT_EVIDENCE,
    )
    report = bind_reviews_to_report(
        _drifted_report(),
        novelty_reviews=[review],
        novelty_points=[_point()],
        evidence_cards=[],
    )

    conclusion = report.conclusions[0]
    assert conclusion.review_status is ReviewStatus.INSUFFICIENT_EVIDENCE
    assert conclusion.verdict is None
    assert conclusion.confidence is None
    assert conclusion.summary == "Coordinator narrative is retained."


@pytest.mark.parametrize(
    ("reviews", "match"),
    [
        ([], "missing review: NP-1"),
        ([_review(), _review()], "duplicate review: NP-1"),
        ([_review(), _review("NP-99")], "unknown review: NP-99"),
    ],
)
def test_binding_rejects_missing_duplicate_and_unknown_reviews(reviews, match):
    with pytest.raises(ValueError, match=match):
        bind_reviews_to_report(
            _drifted_report(),
            novelty_reviews=reviews,
            novelty_points=[_point()],
            evidence_cards=[],
        )


def _execution(execution_id, status, *, source="arxiv", error=None):
    from datetime import datetime, timezone
    from novelty_agent_framework.schemas import SearchExecution

    return SearchExecution(
        execution_id=execution_id,
        tool_name="database_search",
        source_id=source,
        query="query",
        status=status,
        started_at=datetime(2026, 9, 27, tzinfo=timezone.utc),
        error=error,
    )


def _task_result(executions, point_id="NP-1"):
    from novelty_agent_framework.schemas import TaskResearchResult

    return TaskResearchResult(
        task_id="T-1", novelty_point_id=point_id, status="completed",
        steps_used=3, search_executions=executions,
    )


def _unavailable_report(*point_ids):
    return NoveltyReport(
        paper_id="paper-1",
        conclusions=[NoveltyConclusion(
            novelty_point_id=point_id,
            review_status="insufficient_evidence",
            incomplete_reason="material_unavailable",
            summary="Reviewer 核验未完成，尚不能作出新颖性裁定。",
        ) for point_id in point_ids],
        limitations=[
            "保留已确认的范围限制。",
            "本报告节点输入未提供完整检索执行事实，检索覆盖状态未知。",
        ],
    )


def test_search_coverage_reports_all_failed_without_absence_or_error_secrets():
    from novelty_agent_framework.core.report_binding import bind_search_coverage_to_report
    from novelty_agent_framework.schemas import ResearchBundle

    failed = _execution(
        "E-1", "failed",
        error="HTTPStatusError: Client error '406 Not Acceptable' for url "
              "'https://example.test/?api_key=secret-canary'",
    )
    result = _task_result([failed, _execution("E-2", "not_run")])
    # The same execution appears in the bundle and the flattened task audit.
    result.research_bundles = [ResearchBundle(
        bundle_id="B-1", producer="database_search", search_executions=[failed]
    )]
    original = _unavailable_report("NP-1")
    report = bind_search_coverage_to_report(
        original, novelty_points=[_point()], task_research_results=[result]
    )

    text = "\n".join(report.limitations)
    assert "arxiv 成功 0 次、失败 1 次、未执行 1 项" in text
    assert "HTTP 406：1 次" in text
    assert "未获得可供核验的绑定材料" in text
    assert "不能据此推断无相关文献" in text
    assert "状态未知" not in text
    assert "secret-canary" not in text and "example.test" not in text
    assert "NOT_FOUND" not in text and "零命中" not in text
    assert report.conclusions == original.conclusions
    assert original.limitations[-1].endswith("检索覆盖状态未知。")


def test_search_coverage_keeps_mixed_statuses_providers_and_points_separate():
    from novelty_agent_framework.core.report_binding import bind_search_coverage_to_report

    original = _unavailable_report("NP-1", "NP-2", "NP-3")
    report = bind_search_coverage_to_report(
        original,
        novelty_points=[_point("NP-1"), _point("NP-2"), _point("NP-3")],
        task_research_results=[
            _task_result([
                _execution("E-1", "succeeded"),
                _execution("E-2", "failed", error="HTTP 429"),
                _execution("E-3", "partial"),
                _execution("E-4", "requires_human"),
                _execution("E-5", "not_run"),
            ]),
            _task_result([_execution("E-6", "succeeded", source="springer")], "NP-2"),
        ],
    )

    np1 = next(item for item in report.limitations if item.startswith("NP-1："))
    np2 = next(item for item in report.limitations if item.startswith("NP-2："))
    assert "成功 1 次、失败 1 次、未执行 1 项" in np1
    assert "部分成功 1 次、需人工处理 1 次" in np1
    assert "HTTP 429：1 次" in np1 and "springer" not in np1
    assert "springer 成功 1 次、失败 0 次、未执行 0 项" in np2
    assert "HTTP 429" not in np2 and "arxiv" not in np2
    assert "NP-3：未记录检索执行事实，检索覆盖状态未知。" in report.limitations
    assert report.conclusions == original.conclusions


@pytest.mark.parametrize("task_results", [[], [_task_result([])]])
def test_search_coverage_without_executions_remains_unknown(task_results):
    from novelty_agent_framework.core.report_binding import bind_search_coverage_to_report

    report = bind_search_coverage_to_report(
        _unavailable_report("NP-1"),
        novelty_points=[_point()], task_research_results=task_results,
    )
    assert report.limitations == [
        "保留已确认的范围限制。",
        "本报告节点输入未提供完整检索执行事实，检索覆盖状态未知。",
    ]
    assert report.conclusions[0].verdict is None


def test_workflow_binds_existing_task_search_failures_into_report():
    review = NoveltyPointReview(
        novelty_point_id="NP-1", status="insufficient_evidence",
        incomplete_reason="material_unavailable",
    )
    workflow = NoveltyWorkflow.default()
    report = asyncio.run(workflow._synthesize_report({
        "paper": PaperInput(paper_id="paper-1", title="Paper", full_text="body"),
        "novelty_points": [_point()],
        "brief": NoveltyBrief(paper_summary="summary", novelty_points=[_point()]),
        "novelty_reviews": [review],
        "task_research_results": [_task_result([
            _execution("E-1", "failed", error="HTTPStatusError: Client error '406 Not Acceptable'")
        ])],
    }))["report"]

    assert any("HTTP 406：1 次" in item for item in report.limitations)
    assert all("未提供完整检索执行事实" not in item for item in report.limitations)
    assert report.conclusions[0].incomplete_reason == "material_unavailable"
    assert report.conclusions[0].verdict is None
