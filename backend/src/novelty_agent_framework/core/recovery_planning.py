"""Contracts around Coordinator planning; no deterministic task fallback."""
from __future__ import annotations

from ..schemas import ResearchTask
from ..schemas.failures import RecoveryAction, RecoveryAttempt, RecoverySearchFact
from .coordinator_policy import COORDINATOR_WORK_SPECS

SUPPLEMENT_ACTIONS = frozenset({RecoveryAction.RESEARCH_GAP, RecoveryAction.REPLAN_QUERY})
SUPPLEMENT_TASK_TYPES = frozenset({
    "literature_search", "feature_supplement", "language_supplement",
    "research_gap", "replan_query",
})
TRIGGER_TASK_TAGS = {
    "evidence.missing": "COUNT", "review.missing_features": "SEMANTIC", "coverage.no_match": "QUERY",
}
TRIGGER_TASK_TYPES = {code: spec.task_type for code, spec in COORDINATOR_WORK_SPECS.items()}


def decision_key(decision):
    """Point is a target binding; trigger code is the planning workflow identity."""
    return decision.point_id, tuple(decision.trigger_codes)


def recovery_attempts(tasks, results, *, run_id):
    by_key = {(r.novelty_point_id, r.task_id): r for r in results}
    attempts = []
    for task in tasks:
        result = by_key.get((task.novelty_point_id, task.task_id))
        executions, failures = {}, []
        if result:
            for e in [*result.search_executions,
                      *(e for b in result.research_bundles for e in b.search_executions)]:
                if e.run_id in {None, run_id}:
                    executions[(e.execution_id, e.started_at)] = e
            failures = [*result.execution_failures,
                        *(f for b in result.research_bundles for f in b.execution_failures),
                        *(e.failure for e in executions.values() if e.failure)]
        attempts.append(RecoveryAttempt(
            point_id=task.novelty_point_id, task_id=task.task_id, attempt=task.attempt,
            task_type=task.task_type,
            language=task.language, description=task.description.split("\n补检系统上下文：", 1)[0],
            status=result.status.value if result else "result_missing",
            produced_card_count=len(result.evidence_cards) if result else 0,
            failure_codes=list(dict.fromkeys(f.code for f in failures)),
            searches=[RecoverySearchFact(execution_id=e.execution_id, source_id=e.source_id,
                      query=e.query, status=e.status.value, candidate_count=len(e.results))
                      for e in executions.values()],
        ))
    return attempts


def validate_supplement_tasks(tasks, *, brief, decisions, languages, attempt, trigger_code=None):
    """Enforce scope/coverage and assign runtime IDs; never invent model tasks."""
    active = [d for d in decisions if d.action in SUPPLEMENT_ACTIONS]
    allowed = {d.point_id for d in active}
    known = {p.point_id for p in brief.novelty_points}
    if len(allowed) != len(active) or not allowed.issubset(known):
        raise ValueError("RecoveryDecision 必须按已知查新点唯一绑定")
    normalized, seen, counts = [], set(), {}
    for value in tasks:
        task = ResearchTask.model_validate(value)
        if task.novelty_point_id not in allowed:
            raise ValueError(f"补检任务引用了未知查新点或未获准补检的点：{task.novelty_point_id}")
        if task.language not in languages:
            raise ValueError(f"补检任务使用未启用语言：{task.language}")
        if task.task_type not in SUPPLEMENT_TASK_TYPES:
            raise ValueError("Coordinator 只能规划任务级补检，不能安排局部纠错")
        if trigger_code and task.task_type != TRIGGER_TASK_TYPES[trigger_code]:
            raise ValueError(f"{trigger_code} 的任务类型必须为 {TRIGGER_TASK_TYPES[trigger_code]}")
        if not task.description.strip():
            raise ValueError("补检任务必须说明研究方向和所需证据")
        key = (task.novelty_point_id, task.language)
        if key in seen:
            raise ValueError("同一触发码工作流内，每点每语言最多一条补检任务")
        seen.add(key)
        counts[task.novelty_point_id] = counts.get(task.novelty_point_id, 0) + 1
        normalized.append(task.model_copy(update={
            "task_id": (f"T-R{attempt}-{TRIGGER_TASK_TAGS[trigger_code]}-{counts[task.novelty_point_id]}"
                        if trigger_code else f"T-R{attempt}-{counts[task.novelty_point_id]}"), "attempt": attempt,
        }))
    missing = allowed - {t.novelty_point_id for t in normalized}
    if missing:
        raise ValueError(f"Coordinator 未为全部补检决策生成任务：{sorted(missing)}")
    return normalized


def validate_planning_result(outcome, *, brief, decisions, languages, attempt):
    """Check each trigger decision and bind its own tasks, without point-level merging."""
    sources = {decision_key(d): d for d in decisions}
    planned = {decision_key(d): d for d in outcome.recovery_decisions}
    if len(planned) != len(outcome.recovery_decisions) or planned.keys() != sources.keys():
        raise ValueError("Coordinator 必须为全部触发码及目标点返回唯一决策")
    tasks = {(t.novelty_point_id, t.task_id): t for t in outcome.brief.research_tasks}
    if len(tasks) != len(outcome.brief.research_tasks):
        raise ValueError("补检任务 ID 重复")
    owners = {}
    failures = {f.event_id for f in outcome.failure_events}
    model_fields = {"action", "reason", "coordinator_status", "planned_task_ids", "planning_origin",
                    "planning_basis_read_ids", "coordinator_guidance_ids"}
    for key, target in planned.items():
        source = sources[key]
        if target.coordinator_status == "failed":
            excluded = model_fields | {"max_additional_attempts", "cause_event_ids"}
            if (target.action != RecoveryAction.STOP or target.max_additional_attempts != 0
                    or target.model_dump(exclude=excluded) != source.model_dump(exclude=excluded)
                    or not set(source.cause_event_ids).issubset(target.cause_event_ids)
                    or not set(target.cause_event_ids) - set(source.cause_event_ids)
                    or not (set(target.cause_event_ids) - set(source.cause_event_ids)).issubset(failures)):
                raise ValueError("失败触发码决策必须保留事实并绑定本次失败事件")
            continue
        code = source.trigger_codes[0]
        expected_action = RecoveryAction.REPLAN_QUERY if code == "coverage.no_match" else RecoveryAction.RESEARCH_GAP
        if (target.coordinator_status != "planned" or target.action != expected_action
                or target.model_dump(exclude=model_fields) != source.model_dump(exclude=model_fields)
                or target.planning_origin not in {"coordinator_llm", "demo"}):
            raise ValueError("Coordinator 改写了触发事实、预算或动作范围")
        selected = []
        for task_id in target.planned_task_ids:
            address = (target.point_id, task_id)
            if address not in tasks or address in owners:
                raise ValueError("任务未找到或被多个触发码决策重复绑定")
            owners[address] = target
            selected.append(tasks[address])
        normalized = validate_supplement_tasks(selected, brief=brief, decisions=[source],
            languages=languages, attempt=attempt, trigger_code=code)
        if normalized != selected:
            raise ValueError("任务 ID 或轮次不符合触发码绑定")
    if owners.keys() != tasks.keys():
        raise ValueError("存在未绑定决策的补检任务")
    return planned, owners
