"""Select task-level supplementation, with terminal execution guards.

Local repair belongs to the nodes listed in section 13.1 of
docs/后端代码审查与待办清单.md. This policy does not yet verify that every
node has completed its local correction policy.
"""
from __future__ import annotations

from ..schemas.failures import failure_family

from ..schemas.failures import (FailureCode as C, FailureScope, RecoveryAction as A,
    RecoveryDecision, make_failure)


def plan_recovery(*, paper_id, run_id, points, results, reviews, insufficient,
                  round_number, max_rounds, provider_order=(), history=(), current_task_keys=None):
    """Return RESEARCH_GAP / REPLAN_QUERY, or STOP when continuation is blocked.

    provider_order and history remain accepted for caller compatibility only.
    The workflow supplies current_task_keys so old empty queries do not trigger
    a fresh query replan. Coordinator must plan tasks before dispatch.
    """
    decisions, failures = [], {}
    deficient = {item.novelty_point_id for item in insufficient}
    review_by_point = {review.novelty_point_id: review for review in reviews}
    for point in points:
        scope = FailureScope(paper_id=paper_id,run_id=run_id,point_id=point.point_id)
        review = review_by_point.get(point.point_id)
        rows = [r for r in results if r.novelty_point_id == point.point_id]
        executions = {}
        for result in rows:
            for execution in [*(e for b in result.research_bundles for e in b.search_executions), *result.search_executions]:
                executions[(result.task_id, execution.execution_id, execution.started_at)] = execution
        execution_rows = list(executions.values())
        current_executions = [e for (task_id, _, _), e in executions.items()
                              if (current_task_keys is None or (point.point_id, task_id) in current_task_keys)
                              and e.run_id in {None, run_id}]
        # Point-level, current round: at least one succeeded query and zero
        # candidates across ALL queries. Other failed queries remain visible.
        zero_candidates = (any(e.status.value == 'succeeded' for e in current_executions)
                           and not any(e.results for e in current_executions))
        point_failures = [e.failure for e in execution_rows if e.failure is not None]
        point_failures += list(getattr(review, 'execution_issues', [])) if review else []
        for result in rows:
            point_failures += list(getattr(result, 'execution_failures', []))
            point_failures += [failure for bundle in result.research_bundles for failure in bundle.execution_failures]
        failures.update({event.event_id:event for event in point_failures})
        resolved = {record.failure_event_id for row in rows
                    for record in getattr(row, 'correction_records', [])
                    if record.status.value == 'verification_passed'}
        point_failures = [event for event in point_failures if event.event_id not in resolved]
        missing = [f.feature_id for f in getattr(review, 'feature_comparisons', []) if f.relation in {'unknown','partially_supported'}]
        aspects = list(review.supplement_request.missing_aspects) if review and review.supplement_request else []
        semantic_gap = bool(review and review.status.value == 'insufficient_evidence'
                            and review.incomplete_reason == 'semantic_evidence')
        trigger_codes = []
        if point.point_id in deficient:
            trigger_codes.append(C.EVIDENCE_MISSING)
        if semantic_gap:
            trigger_codes.append(C.REVIEW_MISSING_FEATURES)
        if zero_candidates:
            trigger_codes.append(C.COVERAGE_NO_MATCH)
        if not trigger_codes:
            continue
        for code in trigger_codes:
            event = make_failure(code, scope=scope.model_copy(update={'feature_ids':missing}),
                message=f'Task-level supplement trigger: {code.value}; no novelty verdict is implied.',
                occurrence_id=f'round-{round_number}')
            failures[event.event_id]=event
            point_failures.append(event)
        common={'point_id':point.point_id,'cause_event_ids':list(dict.fromkeys(e.event_id for e in point_failures)),
                'missing_feature_ids':list(dict.fromkeys(missing)),'missing_aspects':aspects,
                'trigger_codes':[code.value for code in trigger_codes], 'round_number':round_number,
                'review_reason':review.supplement_request.reason if semantic_gap and review.supplement_request else None,
                'suggested_focus':list(review.supplement_request.suggested_focus)
                    if semantic_gap and review.supplement_request else []}
        def decision(action,reason,**kwargs):
            return RecoveryDecision(**common,action=action,reason=reason,
                max_additional_attempts=0 if action in {A.STOP,A.NONE,A.MANUAL} else 1,**kwargs)
        if round_number >= max_rounds:
            decisions.append(decision(A.STOP,'Configured round limit reached; unresolved evidence remains unresolved.'))
            continue
        if any(failure_family(e.code) == C.BUDGET_EXHAUSTED for e in point_failures):
            decisions.append(decision(A.STOP,'An explicit execution budget denial prevents automatic continuation.'))
            continue
        if (review and review.incomplete_reason in {'technical_error','budget_exhausted'}
                and (any(row.evidence_cards for row in rows) or any(
                    item.novelty_point_id==point.point_id and item.valid_card_count>0 for item in insufficient))):
            # Summary recovery is a separate path; do not waste another research
            # round to compensate for Reviewer execution failure.
            decisions.append(decision(A.STOP,'Reviewer execution is incomplete; recover its saved card/summary state instead of repeating literature searches.'))
            continue
        if any(failure_family(event.code) in {C.MODEL_TIMEOUT,C.MODEL_TRANSPORT,C.MODEL_CONTEXT_LIMIT,
                              C.MODEL_CONTEXT_UNAVAILABLE,C.MODEL_OUTPUT_SCHEMA} for event in point_failures):
            decisions.append(decision(A.STOP,'Researcher execution is incomplete; restore its validated checkpoint or resolve the recorded model/context failure before starting new literature research.'))
            continue
        attempted=[e for e in execution_rows if e.status.value!='not_run']
        successful=[e for e in attempted if e.status.value in {'succeeded','partial'}]
        failed=[e for e in attempted if e.status.value in {'failed','requires_human'}]
        if failed and not successful:
            decisions.append(decision(A.STOP,'Search is technically blocked; provider switching and request retries belong to local correction, not task-level supplementation. This is not a no-match finding.'))
            continue
        for code in trigger_codes:
            action = A.REPLAN_QUERY if code == C.COVERAGE_NO_MATCH else A.RESEARCH_GAP
            reason = {
                C.REVIEW_MISSING_FEATURES: 'Reviewer has a documented semantic evidence gap.',
                C.COVERAGE_NO_MATCH: 'Successful queries returned zero candidates; a new search direction is needed.',
                C.EVIDENCE_MISSING: 'Final valid evidence remains below the configured threshold.',
            }[code]
            causes = [e.event_id for e in point_failures if e.code not in trigger_codes or e.code == code]
            decisions.append(RecoveryDecision(**{**common, 'trigger_codes': [code.value],
                                                'cause_event_ids': list(dict.fromkeys(causes))},
                                              action=action, reason=reason))
    return decisions,list(failures.values())
