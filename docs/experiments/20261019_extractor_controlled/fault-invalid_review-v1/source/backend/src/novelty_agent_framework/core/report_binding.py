"""Deterministic Reviewer-to-report authority binding."""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Sequence

from ..schemas import (
    EvidenceCard,
    NoveltyConclusion,
    NoveltyPoint,
    NoveltyPointReview,
    NoveltyReport,
    ReportNarrativeDraft,
    TaskResearchResult,
)


_SEARCH_COVERAGE_UNKNOWN = "本报告节点输入未提供完整检索执行事实，检索覆盖状态未知。"
_HTTP_STATUS = re.compile(
    r"(?:\b(?:Client|Server) error [\"']|\bHTTP(?: status)?[ :=]*)([1-5]\d{2})\b",
    re.IGNORECASE,
)


def assemble_report_from_draft(
    draft: ReportNarrativeDraft,
    *,
    paper_id: str,
    novelty_reviews: Sequence[NoveltyPointReview],
    novelty_points: Sequence[NoveltyPoint],
    evidence_cards: Sequence[EvidenceCard],
    rejected_evidence: Sequence[str] = (),
) -> NoveltyReport:
    """Assemble a complete report without asking the model to repeat authority fields."""

    point_counts = Counter(point.point_id for point in novelty_points)
    review_counts = Counter(review.novelty_point_id for review in novelty_reviews)
    narrative_counts = Counter(item.novelty_point_id for item in draft.conclusions)
    if any(count != 1 for count in point_counts.values()):
        raise ValueError("novelty_points must contain unique point_id values")
    _require_exact_coverage("review", point_counts, review_counts)
    _require_exact_coverage("conclusion", point_counts, narrative_counts)
    cards_by_id = _unique_cards(evidence_cards)
    reviews_by_id = {item.novelty_point_id: item for item in novelty_reviews}
    narrative_by_id = {item.novelty_point_id: item for item in draft.conclusions}
    conclusions = []
    # Free-form model limitations can imply unrecorded search coverage or causes.
    # Keep the draft in the raw response, but publish only traceable state here.
    limitations: list[str] = []
    for point in novelty_points:
        review = reviews_by_id[point.point_id]
        narrative = narrative_by_id[point.point_id]
        _validate_card_groups(narrative, cards_by_id)
        values = {
            "novelty_point_id": point.point_id,
            "summary": narrative.summary,
            "supporting_card_ids": list(narrative.supporting_card_ids),
            "counter_card_ids": list(narrative.counter_card_ids),
            **_review_fields(review),
        }
        if review.status.value == "insufficient_evidence":
            values["summary"] = _limited_summary(review)
            limitations.append(
                f"{point.point_id}：Reviewer 核验未完成；原因："
                f"{review.incomplete_reason or '未记录'}。"
            )
        if review.supplement_request is not None:
            limitations.append(
                f"{point.point_id}：Reviewer 提出补查请求；原始请求保留在来源 Review，"
                "本报告节点未执行补查。"
            )
        conclusions.append(NoveltyConclusion.model_validate(values))
    for rejected in rejected_evidence:
        limitations.append(
            f"被拒绝证据：{rejected}" if ": " in rejected
            else f"被拒绝证据：{rejected}；来源未提供拒绝原因。"
        )
    limitations.append(_SEARCH_COVERAGE_UNKNOWN)
    return NoveltyReport.model_validate({
        "paper_id": paper_id,
        "conclusions": conclusions,
        "limitations": list(dict.fromkeys(limitations)),
    })


def bind_search_coverage_to_report(
    report: NoveltyReport,
    *,
    novelty_points: Sequence[NoveltyPoint],
    task_research_results: Sequence[TaskResearchResult],
) -> NoveltyReport:
    """Publish recorded execution states without converting failures into absence.

    SearchExecution counts are logical query executions, not physical HTTP counts.
    Only recognized HTTP status numbers leave the error field; raw errors/URLs do not.
    """
    limitations = [item for item in report.limitations if item != _SEARCH_COVERAGE_UNKNOWN]
    conclusions = {item.novelty_point_id: item for item in report.conclusions}
    by_point = {}
    for point in novelty_points:
        executions = {}
        for result in task_research_results:
            if result.novelty_point_id != point.point_id:
                continue
            rows = [execution for bundle in result.research_bundles
                    for execution in bundle.search_executions]
            for execution in [*rows, *result.search_executions]:
                key = (result.task_id, execution.execution_id, execution.started_at)
                executions[key] = execution
        by_point[point.point_id] = list(executions.values())
    if not any(by_point.values()):
        limitations.append(_SEARCH_COVERAGE_UNKNOWN)
    else:
        for point_id, executions in by_point.items():
            if not executions:
                limitations.append(f"{point_id}：未记录检索执行事实，检索覆盖状态未知。")
                continue
            source_summaries = []
            for source_id in sorted({item.source_id for item in executions}):
                source_rows = [item for item in executions if item.source_id == source_id]
                counts = Counter(item.status.value for item in source_rows)
                summary = (
                    f"{source_id} 成功 {counts['succeeded']} 次、"
                    f"失败 {counts['failed']} 次、未执行 {counts['not_run']} 项"
                )
                for status, label in (("partial", "部分成功"), ("requires_human", "需人工处理")):
                    if counts[status]:
                        summary += f"、{label} {counts[status]} 次"
                codes = Counter(
                    match.group(1) for item in source_rows
                    if item.status.value not in {"succeeded", "not_run"}
                    and (match := _HTTP_STATUS.search(item.error or ""))
                )
                if codes:
                    summary += "（已记录 " + "、".join(
                        f"HTTP {code}：{count} 次" for code, count in sorted(codes.items())
                    ) + "）"
                source_summaries.append(summary)
            message = f"{point_id}：已记录检索执行：" + "；".join(source_summaries) + "。"
            if any(item.status.value == "failed" for item in executions):
                conclusion = conclusions.get(point_id)
                if conclusion is not None and conclusion.incomplete_reason == "material_unavailable":
                    message += "存在检索执行失败，且未获得可供核验的绑定材料；不能据此推断无相关文献。"
                else:
                    message += "存在检索执行失败；不能据此推断无相关文献。"
            message += "以上仅为已记录的执行范围，不代表完整检索或新颖性裁定。"
            limitations.append(message)
    return report.model_copy(update={"limitations": list(dict.fromkeys(limitations))})


def bind_reviews_to_report(
    draft: NoveltyReport,
    *,
    novelty_reviews: Sequence[NoveltyPointReview],
    novelty_points: Sequence[NoveltyPoint],
    evidence_cards: Sequence[EvidenceCard],
) -> NoveltyReport:
    """Replace every report judgment field with the corresponding Reviewer value."""

    del evidence_cards  # Reserved for deterministic card-binding checks.
    point_counts = Counter(point.point_id for point in novelty_points)
    if any(count != 1 for count in point_counts.values()):
        raise ValueError("novelty_points must contain unique point_id values")
    review_counts = Counter(review.novelty_point_id for review in novelty_reviews)
    conclusion_counts = Counter(
        conclusion.novelty_point_id for conclusion in draft.conclusions
    )
    _require_exact_coverage("review", point_counts, review_counts)
    _require_exact_coverage("conclusion", point_counts, conclusion_counts)

    reviews_by_id = {
        review.novelty_point_id: review for review in novelty_reviews
    }
    conclusions_by_id = {
        conclusion.novelty_point_id: conclusion for conclusion in draft.conclusions
    }
    conclusions = []
    for point in novelty_points:
        review = reviews_by_id[point.point_id]
        conclusion = conclusions_by_id[point.point_id]
        values = conclusion.model_dump(mode="python")
        values.update(_review_fields(review))
        if review.status.value == "insufficient_evidence":
            values["summary"] = _limited_summary(review)
        conclusions.append(NoveltyConclusion.model_validate(values))
    values = draft.model_dump(mode="python")
    values["conclusions"] = conclusions
    return NoveltyReport.model_validate(values)


def _review_fields(review: NoveltyPointReview) -> dict:
    return {
        "review_status": review.status,
        "verdict": review.verdict,
        "verdict_reason": review.verdict_reason,
        "confidence": review.confidence,
        "highly_relevant_works": list(review.highly_relevant_works),
        "review_evidence": list(review.review_evidence),
        "feature_comparisons": list(review.feature_comparisons),
        "incomplete_reason": review.incomplete_reason,
    }


def _limited_summary(review: NoveltyPointReview) -> str:
    return (
        "Reviewer 核验未完成，尚不能作出新颖性裁定。"
        if review.incomplete_reason in {"budget_exhausted", "technical_error", "material_unavailable"}
        else "该查新点的关键比较证据不足，尚不能作出新颖性裁定。"
    )


def _unique_cards(evidence_cards: Sequence[EvidenceCard]) -> dict[str, EvidenceCard]:
    counts = Counter(card.card_id for card in evidence_cards)
    duplicate = [card_id for card_id, count in counts.items() if count > 1]
    if duplicate:
        raise ValueError(f"duplicate authorized card: {', '.join(duplicate)}")
    return {card.card_id: card for card in evidence_cards}


def _validate_card_groups(narrative, cards_by_id: dict[str, EvidenceCard]) -> None:
    supporting = narrative.supporting_card_ids
    counter = narrative.counter_card_ids
    if len(set(supporting)) != len(supporting) or len(set(counter)) != len(counter):
        raise ValueError(f"duplicate card reference: {narrative.novelty_point_id}")
    if set(supporting) & set(counter):
        raise ValueError(f"supporting/counter conflict: {narrative.novelty_point_id}")
    for card_id in [*supporting, *counter]:
        card = cards_by_id.get(card_id)
        if card is None:
            raise ValueError(f"unknown card reference: {card_id}")
        if card.novelty_point_id != narrative.novelty_point_id:
            raise ValueError(
                f"cross-point reference: {narrative.novelty_point_id} -> {card_id}"
            )


def _require_exact_coverage(
    label: str,
    expected: Counter[str],
    actual: Counter[str],
) -> None:
    problems = []
    for point_id in expected:
        if actual[point_id] == 0:
            problems.append(f"missing {label}: {point_id}")
        elif actual[point_id] > 1:
            problems.append(f"duplicate {label}: {point_id}")
    for point_id in actual.keys() - expected.keys():
        problems.append(f"unknown {label}: {point_id}")
    if problems:
        raise ValueError("; ".join(problems))


def bind_point_lifecycle_to_report(report, *, state):
    """Track every extracted candidate through execution without asserting coverage."""
    rows = []
    reviews = {review.novelty_point_id: review for review in state.get('novelty_reviews', [])}
    conclusions = {row.novelty_point_id: row for row in report.conclusions}
    for point in state.get('novelty_points', []):
        pid = point.point_id
        tasks = [t for t in state.get('all_research_tasks', []) if t.novelty_point_id == pid]
        plans = [p for p in state.get('search_plans', []) if p.novelty_point_id == pid]
        results = [r for r in state.get('task_research_results', []) if r.novelty_point_id == pid]
        cards = [c for c in state.get('evidence_cards', []) if c.novelty_point_id == pid]
        review, conclusion = reviews.get(pid), conclusions.get(pid)
        gaps = []
        if not tasks: gaps.append('task_not_created')
        planned = {p.task_id for p in plans}
        completed = {r.task_id for r in results}
        if any(t.task_id not in planned for t in tasks): gaps.append('search_plan_missing')
        if any(t.task_id not in completed for t in tasks): gaps.append('task_result_missing')
        if not cards: gaps.append('validated_evidence_missing')
        if review is None: gaps.append('review_missing')
        elif review.incomplete_reason: gaps.append('review_' + review.incomplete_reason)
        if conclusion is None: gaps.append('report_conclusion_missing')
        rows.append({'point_id':pid, 'task_ids':[t.task_id for t in tasks],
            'planned_task_ids':sorted(planned), 'result_task_ids':sorted(completed),
            'raw_card_ids':[c.card_id for r in results for c in r.evidence_cards],
            'validated_card_ids':[c.card_id for c in cards],
            'review_status':review.status.value if review else None,
            'review_incomplete_reason':review.incomplete_reason if review else None,
            'report_present':conclusion is not None, 'gaps':gaps,
            'semantic_scope_complete':None})
    return report.model_copy(update={'point_lifecycle':rows})
