"""Choose bounded next work from execution facts and unresolved evidence needs."""
from __future__ import annotations

from ..schemas.failures import failure_family

from ..schemas.failures import (FailureCode as C, FailureScope, RecoveryAction as A,
    RecoveryArtifact, RecoveryDecision, make_failure)


def plan_recovery(*, paper_id, run_id, points, results, reviews, insufficient,
                  round_number, max_rounds, provider_order=(), history=()):
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
        point_failures = [e.failure for e in execution_rows if e.failure is not None]
        point_failures += list(getattr(review, 'execution_issues', [])) if review else []
        for result in rows:
            point_failures += list(getattr(result, 'execution_failures', []))
            point_failures += [failure for bundle in result.research_bundles for failure in bundle.execution_failures]
        failures.update({event.event_id:event for event in point_failures})
        missing = [f.feature_id for f in getattr(review, 'feature_comparisons', []) if f.relation in {'unknown','partially_supported'}]
        aspects = list(review.supplement_request.missing_aspects) if review and review.supplement_request else []
        semantic_gap = bool(review and review.status.value == 'insufficient_evidence'
                            and review.incomplete_reason == 'semantic_evidence')
        if point.point_id not in deficient and not semantic_gap:
            continue
        if semantic_gap:
            event = make_failure(C.REVIEW_MISSING_FEATURES,scope=scope.model_copy(update={'feature_ids':missing}),
                                 message='Reviewer has unresolved semantic evidence needs.',occurrence_id=f'round-{round_number}')
            failures[event.event_id]=event
            point_failures.append(event)
        else:
            event=make_failure(C.EVIDENCE_MISSING,scope=scope,message='Final evidence count is below the configured threshold.',occurrence_id=f'round-{round_number}')
            failures[event.event_id]=event
            point_failures.append(event)
        common={'point_id':point.point_id,'cause_event_ids':list(dict.fromkeys(e.event_id for e in point_failures)),
                'missing_feature_ids':list(dict.fromkeys(missing)),'missing_aspects':aspects}
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
        # Reuse known, unread materials before any new query. Failed/excluded reads
        # are not manufactured into artifacts or evidence.
        already_read={(r.namespace.value,r.artifact_id) for row in rows for r in row.read_results}
        candidates={}
        for row in rows:
            for item in row.candidate_audit:
                if item.status == 'excluded': continue
                for artifact_id in item.artifact_ids:
                    handle=RecoveryArtifact(namespace=item.namespace,artifact_id=artifact_id)
                    if (handle.namespace,artifact_id) not in already_read:
                        candidates[(handle.namespace,artifact_id)]=handle
            for bundle in row.research_bundles:
                for artifact in bundle.artifacts:
                    if artifact.role.value in {'abstract','full_text','extracted_text'} and ('research_reference',artifact.artifact_id) not in already_read:
                        candidates[('research_reference',artifact.artifact_id)]=RecoveryArtifact(artifact_id=artifact.artifact_id)
        previously_attempted={(a.namespace,a.artifact_id) for d in history
            if d.point_id==point.point_id and d.action==A.READ_ARTIFACT for a in d.artifacts}
        exhausted_known_reads=bool(candidates) and set(candidates).issubset(previously_attempted)
        candidates={key:handle for key,handle in candidates.items() if key not in previously_attempted}
        if exhausted_known_reads:
            decisions.append(decision(A.STOP,'The bound unread material recovery was already attempted; inspect the recorded read failure instead of repeating it.'))
            continue
        if candidates:
            decisions.append(decision(A.READ_ARTIFACT,'Known candidate material has not been read; no new search is needed.',artifacts=list(candidates.values())))
            continue
        attempted=[e for e in execution_rows if e.status.value!='not_run']
        successful=[e for e in attempted if e.status.value in {'succeeded','partial'}]
        failed=[e for e in attempted if e.status.value in {'failed','requires_human'}]
        if failed and not successful:
            used={e.source_id for e in attempted}
            alternatives=[source for source in provider_order if source not in used]
            if alternatives:
                decisions.append(decision(A.CHANGE_PROVIDER,'All attempted searches failed; use the next explicitly configured source with the existing query plan.',source_id=alternatives[0]))
            else:
                transient=[e for e in failed if e.failure and e.failure.retry.allowed and failure_family(e.failure.code) in {C.PROVIDER_NETWORK,C.PROVIDER_RATE_LIMIT,C.PROVIDER_SERVICE}]
                previous_retries={d.source_id for d in history if d.point_id==point.point_id and d.action==A.RETRY_REQUEST}
                retry=next((e for e in transient if e.source_id not in previous_retries),None)
                if retry:
                    decisions.append(decision(A.RETRY_REQUEST,'Retry a transient failed source once, using the same plan and existing transport backoff.',source_id=retry.source_id))
                else:
                    decisions.append(decision(A.STOP,'Search is technically blocked; no unused configured alternative or bounded retry is available. This is not a no-match finding.'))
            continue
        if semantic_gap and rows:
            by_source={}
            for row in rows:
                for bundle in row.research_bundles:
                    full_works={a.work_id for a in bundle.artifacts if a.role.value in {'full_text','extracted_text'}}
                    for record in bundle.source_records:
                        if record.work_id not in full_works:
                            by_source.setdefault(record.source_id,[]).append(record.source_record_id)
            acquired={record for d in history if d.point_id==point.point_id and d.action==A.FETCH_FULLTEXT for record in d.source_record_ids}
            for source, ids in by_source.items():
                selected=[i for i in dict.fromkeys(ids) if i not in acquired][:4]
                if selected:
                    decisions.append(decision(A.FETCH_FULLTEXT,'Existing abstracts do not resolve the Reviewer gap; acquire these bound records without repeating search.',source_id=source,source_record_ids=selected))
                    break
            else:
                decisions.append(decision(A.RESEARCH_GAP,'Known material has not resolved the documented technical feature gap.'))
            continue
        if successful and not any(e.results for e in successful):
            event=make_failure(C.COVERAGE_NO_MATCH,scope=scope,message='Completed queries returned no candidates; this does not establish novelty.',occurrence_id=f'round-{round_number}')
            failures[event.event_id]=event
            common["cause_event_ids"].append(event.event_id)
            decisions.append(decision(A.REPLAN_QUERY,'Successful queries returned zero candidates; a new semantic search direction is needed.'))
        else:
            decisions.append(decision(A.RESEARCH_GAP,'Evidence remains below the configured threshold; plan only the unresolved point.'))
    return decisions,list(failures.values())
