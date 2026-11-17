"""Project-report projection of verified Planner audit; no diagnostic paths/logs."""
import json

from .concept_audit import read_published_attempts


def statuses_for_plans(plans, tasks=()):
    from .runtime_artifacts import current_runtime_artifacts
    runtime=current_runtime_artifacts()
    if not runtime or not runtime.run_dir or not (runtime.run_dir/'planner/index.json').exists():
        return []
    result=[]
    planned = {(plan.novelty_point_id, plan.task_id): plan for plan in plans}
    requested = dict.fromkeys([*planned, *((task.novelty_point_id, task.task_id) for task in tasks)])
    for point_id, task_id in requested:
        plan = planned.get((point_id, task_id))
        records=read_published_attempts(runtime.run_dir/'planner',run_id=runtime.run_id,
            point_id=point_id,task_id=task_id)
        matched=None
        if plan is None:
            if records:
                latest = json.loads(records[-1]['summary.json'])
                if latest['status'] in ('failed', 'cancelled'):
                    matched = latest
        else:
            for record in reversed(records):
                if 'plan.json' in record and json.loads(record['plan.json'])==plan.model_dump(mode='json'):
                    matched=json.loads(record['summary.json'])
                    break
        if matched is None:
            result.append(dict(task_id=task_id,novelty_point_id=point_id,status='unverified'))
            continue
        strategies=matched.get('strategy_result',{})
        result.append(dict(task_id=task_id,novelty_point_id=point_id,
            status=matched['status'],unresolved_concept_ids=sorted(matched['concept_result'].get('unresolved',{})),
            excluded_semantic_fact_ids=sorted(matched.get('semantic_exclusions', {})),
            failed_levels=sorted(strategies.get('failed_levels',{})),
            accepted_levels=[s.level for s in plan.strategies] if plan is not None else [],
            strategy_review_performed=strategies.get('review_performed',bool(matched.get('strategy_calls'))),
            semantic_standard='automatic' if matched.get('semantic_contract_id') else 'not_recorded'))
    return result


def planner_status_notes(point_plans):
    names={'strict':'严格','medium':'适中','broad':'宽泛'}
    notes=[]
    for point_plan in point_plans:
        for row in point_plan.get('planner_statuses',[]):
            prefix=f"{row['novelty_point_id']}／{row['task_id']}："
            if row['status']=='unverified':
                notes.append(prefix+'规划审计未与当前计划核验，不能确认规划完整性。')
                continue
            if row['status']!='completed':
                label={'partial':'规划部分完成','failed':'规划失败','cancelled':'规划已取消'}.get(row['status'],'规划状态未确认')
                detail=f"{label}；未解决概念{len(row.get('unresolved_concept_ids',[]))}项"
                if row.get('unresolved_concept_ids'):
                    detail+='（候选编号：'+'、'.join(row['unresolved_concept_ids'])+'）'
                if row.get('excluded_semantic_fact_ids'):
                    detail+='；动作未确认或被拒绝的事实：'+'、'.join(row['excluded_semantic_fact_ids'])
                if row.get('failed_levels'):
                    detail+='；未通过档位：'+'、'.join(names.get(v,v) for v in row['failed_levels'])
                notes.append(prefix+detail+'。')
            if not row.get('strategy_review_performed'):
                notes.append(prefix+'检索策略未记录独立语义复核，不能把策略生成成功视为语义通过。')
    return notes
