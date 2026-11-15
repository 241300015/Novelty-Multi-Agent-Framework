"""Task-local concept review and bounded correction."""
from dataclasses import asdict
from typing import Literal

from pydantic import Field

from ..schemas.domain import StrictModel
from ..schemas.search_plan_draft import SearchConceptDraft


class ConceptBatch(StrictModel):
    concepts: list[SearchConceptDraft] = Field(min_length=1)
    comparison: list[SearchConceptDraft] = Field(default_factory=list)


class TermReview(StrictModel):
    term: str
    substitutable: bool
    reason: str = Field(min_length=1)


class AliasReview(StrictModel):
    alias: str
    substitutable: bool
    reason: str = Field(min_length=1)


class Verdict(StrictModel):
    candidate: int
    source_supported: bool
    source_quote: str
    source_reason: str = Field(min_length=1)
    role_correct: bool
    role_reason: str = Field(min_length=1)
    translation_consistent: bool | None
    translation_reason: str
    terms: list[TermReview]
    aliases: list[AliasReview]
    issue_resolution: Literal["fixed", "invalid", "unresolved"] | None = None
    resolution_reason: str = ""


class Review(StrictModel):
    decisions: list[Verdict]
    missing: list[str]


class Correction(StrictModel):
    concept: SearchConceptDraft


def validate_review(result, payload):
    """Reject missing or invented field judgments and unsupported source quotes."""
    items = payload["candidates"]
    ids = [str(verdict.candidate) for verdict in result.decisions]
    if sorted(ids) != sorted(items) or (not payload["check_coverage"] and result.missing):
        raise ValueError("复核未逐项覆盖候选，或局部复核越界")
    point = payload["point"]
    sources = [point["claim"], point["claim_en"],
               *point["technical_features"], *point["technical_features_en"]]
    for verdict in result.decisions:
        concept = items[str(verdict.candidate)]
        if sorted(item.term for item in verdict.terms) != sorted(concept["terms"]):
            raise ValueError("terms 复核未覆盖候选词项")
        if sorted(item.alias for item in verdict.aliases) != sorted(concept["alias"]):
            raise ValueError("aliases 复核未覆盖候选别名")
        if verdict.source_supported and (
            not verdict.source_quote.strip() or
            not any(verdict.source_quote in source for source in sources)
        ):
            raise ValueError(f"候选 {verdict.candidate} 的 source_quote {verdict.source_quote!r} 不在输入查新点或技术特征中；请从输入原文直接复制连续片段")
        cross = bool(payload["comparison"])
        if cross and (verdict.translation_consistent is None or not verdict.translation_reason.strip()):
            raise ValueError("补译时必须判定跨语言一致性并给出理由")
        if not cross and verdict.translation_consistent is not None:
            raise ValueError("不需要补译时跨语言一致性必须为 null")
        if not payload["check_coverage"] and (
            verdict.issue_resolution is None or not verdict.resolution_reason.strip()
        ):
            raise ValueError("局部复核必须说明原问题处理结果及理由")


def verdict_problem(verdict):
    reasons = []
    if not verdict.source_supported:
        reasons.append(f"查新点缺少支撑: {verdict.source_reason}")
    if not verdict.role_correct:
        reasons.append(f"概念分类: {verdict.role_reason}")
    if verdict.translation_consistent is False:
        reasons.append(f"补译一致性: {verdict.translation_reason}")
    if len(verdict.terms) > 1:
        reasons.extend(f"词项 {item.term}: {item.reason}" for item in verdict.terms
                       if not item.substitutable)
    reasons.extend(f"别名 {item.alias}: {item.reason}" for item in verdict.aliases
                   if not item.substitutable)
    return "；".join(reasons)


async def run_concept_stage(agent, point, task, call, record):
    v2 = getattr(agent, 'concept_review_protocol', 'v1') == 'v2'
    issue_details = {}
    mechanical_fields = {}
    source = {"point": point.model_dump(), "task": task.model_dump()}
    cross = not (point.claim_en if task.language.lower().startswith("en") else point.claim)
    from ..agents.search_plan_compiler import SemanticLimits, validate_concept_semantics
    source["limits"] = asdict(agent.semantic_limits or SemanticLimits())
    source_catalog = None
    if v2:
        from .concept_evidence import build_catalog
        source_catalog = build_catalog(point, task)
        record(0, 'concept_source_catalog', source_catalog)
    batch = await agent.generate_concepts(call, {**source, "needs_translation": cross})
    if bool(batch.comparison) != cross:
        raise ValueError("来源语言对照与任务语言条件不一致")
    context = {**source, "comparison": [c.model_dump() for c in batch.comparison]}
    if v2:
        context['source_catalog'] = source_catalog
    candidates = dict(enumerate(batch.concepts, 1))
    passed = {}

    async def assess(items, whole, *, problem=None, previous=None):
        payload = {**context, "check_coverage": whole,
                   "candidates": {str(k): v.model_dump() for k, v in items.items()}}
        if not whole:
            payload["problem"] = problem
            payload["previous_candidate"] = previous.model_dump() if previous else None
            if v2:
                payload['issues'] = issue_details.get(next(iter(items)), [])
        result = await agent.review_concepts(call, payload)
        validate_review(result, payload)
        catalog = items if whole else {**passed, **items}
        keys = list(catalog)
        mechanical = {key: [] for key in items}
        for key in items:
            mechanical_fields[key] = set()
        for issue in validate_concept_semantics(list(catalog.values()), task=task, limits=agent.semantic_limits):
            for path in issue.paths:
                if issue.code == "too_many_concepts":
                    continue
                if len(path) < 2:
                    if issue.code == "missing_escape" and whole:
                        result.missing.append(issue.fix)
                        continue
                    raise ValueError(issue.detail)
                target = keys[path[1]]
                for key in items:
                    if key == target or (not whole and target in passed):
                        mechanical[key].append(issue.format())
                        if len(path) > 2 and path[2] in ('terms','alias','exclude','role','importance'):
                            mechanical_fields[key].add(path[2])
        return result, mechanical

    initial, mechanical = await assess(candidates, True)
    issues = {}
    capacity_blocked = set()
    for verdict in sorted(initial.decisions, key=lambda v: v.candidate):
        if v2:
            # Optional aliases cannot force an otherwise valid main concept to
            # be rewritten. Keep only aliases independently judged equivalent.
            rejected = [a for a in verdict.aliases if not a.substitutable]
            if rejected:
                old = candidates[verdict.candidate]
                candidates[verdict.candidate] = old.model_copy(update={'alias': [a.alias for a in verdict.aliases if a.substitutable]})
                for i, alias in enumerate(rejected, 1):
                    record(0, 'concept_alias_quarantined', dict(issue_id=f'I-C{verdict.candidate}-excluded-alias-{i}',
                        candidate=verdict.candidate, alias=alias.alias, reason=alias.reason, status='excluded'))
                verdict.aliases = [a for a in verdict.aliases if a.substitutable]
        problem = verdict_problem(verdict)
        problems = [problem, *mechanical[verdict.candidate]]
        problem = "；".join(p for p in problems if p)
        if not problem and len(passed) >= source["limits"]["max_concepts"]:
            problem = "通过集已达概念数量上限，候选未放行"
            capacity_blocked.add(verdict.candidate)
        if not problem:
            passed[verdict.candidate] = candidates[verdict.candidate]
        else:
            issues[verdict.candidate] = problem
            if v2:
                from .concept_repair_v2 import semantic_issues
                issue_details[verdict.candidate] = semantic_issues(verdict, candidates[verdict.candidate], mechanical[verdict.candidate], mechanical_fields[verdict.candidate])
    for key, reason in enumerate(initial.missing, len(candidates) + 1):
        issues[key] = reason
        if v2:
            issue_details[key] = [dict(issue_id=f'I-C{key}-missing', candidate_id=f'C{key}',
                field_path=[], code='missing', reason=reason, allowed_fields=['terms','alias','role','exclude','importance'], status='open')]

    def record_progress(round_number):
        record(round_number, "concept_review_progress", {
            "accepted": sorted(passed),
            "accepted_concepts": {str(key): value.model_dump() for key, value in passed.items()},
            "unresolved": dict(issues),
            **({'issues': issue_details} if v2 else {}),
        })

    record_progress(0)
    for round_number in range(1, 4):
        if not issues:
            break
        for key, reason in list(issues.items()):
            if key in capacity_blocked:
                continue
            old = candidates.get(key)
            payload = {**context, "problem": reason, "candidate": old.model_dump() if old else None}
            if v2:
                payload['issues'] = issue_details.get(key, [])
            update = await agent.correct_concept(call, payload)
            candidates[key] = update.concept
            review, mechanical = await assess({key: update.concept}, False, problem=reason, previous=old)
            verdict = review.decisions[0]
            changed = old is None or old != update.concept
            problem = "；".join(p for p in [verdict_problem(verdict), *mechanical[key]] if p)
            resolution = verdict.issue_resolution
            if resolution == "fixed" and not changed:
                problem = f"{reason}；修正内容未改变，不能认定问题已修复"
            elif resolution == "unresolved":
                problem = f"{reason}；复核仍未解决：{verdict.resolution_reason}"
            if not problem and len(passed) >= source["limits"]["max_concepts"]:
                problem = f"{reason}；通过集已达概念数量上限，候选未放行"
                capacity_blocked.add(key)
            if problem:
                issues[key] = problem
            else:
                passed[key] = update.concept
                del issues[key]
            if v2:
                if problem:
                    from .concept_repair_v2 import semantic_issues
                    existing = {d['issue_id'] for d in issue_details.get(key, [])}
                    issue_details.setdefault(key, []).extend(d for d in semantic_issues(verdict, update.concept, mechanical[key], mechanical_fields[key])
                        if d['issue_id'] not in existing)
                for detail in issue_details.get(key, []):
                    detail['status'] = 'unresolved' if problem else resolution
            record(round_number, "concept_issue_resolved", {
                "candidate": key, "problem": reason,
                "previous_candidate": old.model_dump() if old else None,
                "candidate_after": update.concept.model_dump(), "changed": changed,
                "resolution": resolution if not problem else "unresolved",
                "review_resolution": resolution, "reason": verdict.resolution_reason,
                "remaining_problem": problem,
                **({'issue_ids': [d['issue_id'] for d in issue_details.get(key, [])]} if v2 else {}),
            })
            record_progress(round_number)
    record(0, "concept_stage_finished", {
        "accepted": sorted(passed),
        "accepted_concepts": {str(key): value.model_dump() for key, value in passed.items()},
        "unresolved": dict(issues),
    })
    if not passed:
        raise ValueError("概念复核通过集为空")
    return [passed[key].model_dump() for key in sorted(passed)]
