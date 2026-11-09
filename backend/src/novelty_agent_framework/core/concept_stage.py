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
            raise ValueError("来源依据必须逐字出现在输入查新点或技术特征中")
        if not payload["check_coverage"] and (
            verdict.issue_resolution is None or not verdict.resolution_reason.strip()
        ):
            raise ValueError("局部复核必须说明原问题处理结果及理由")


def verdict_problem(verdict):
    reasons = []
    if not verdict.source_supported:
        reasons.append("查新点缺少支撑")
    reasons.extend(f"词项 {item.term}: {item.reason}" for item in verdict.terms
                   if not item.substitutable)
    reasons.extend(f"别名 {item.alias}: {item.reason}" for item in verdict.aliases
                   if not item.substitutable)
    return "；".join(reasons)


async def run_concept_stage(agent, point, task, call, record):
    source = {"point": point.model_dump(), "task": task.model_dump()}
    cross = not (point.claim_en if task.language.lower().startswith("en") else point.claim)
    from ..agents.search_plan_compiler import SemanticLimits, validate_concept_semantics
    source["limits"] = asdict(agent.semantic_limits or SemanticLimits())
    batch = await agent.generate_concepts(call, {**source, "needs_translation": cross})
    if bool(batch.comparison) != cross:
        raise ValueError("来源语言对照与任务语言条件不一致")
    context = {**source, "comparison": [c.model_dump() for c in batch.comparison]}
    candidates = dict(enumerate(batch.concepts, 1))
    passed = {}

    async def assess(items, whole, *, problem=None, previous=None):
        payload = {**context, "check_coverage": whole,
                   "candidates": {str(k): v.model_dump() for k, v in items.items()}}
        if not whole:
            payload["problem"] = problem
            payload["previous_candidate"] = previous.model_dump() if previous else None
        result = await agent.review_concepts(call, payload)
        validate_review(result, payload)
        catalog = items if whole else {**passed, **items}
        keys = list(catalog)
        mechanical = {key: [] for key in items}
        for issue in validate_concept_semantics(list(catalog.values()), task=task, limits=agent.semantic_limits):
            for path in issue.paths:
                if len(path) < 2:
                    if issue.code == "missing_escape" and whole:
                        result.missing.append(issue.fix)
                        continue
                    raise ValueError(issue.detail)
                target = keys[path[1]]
                for key in items:
                    if key == target or (not whole and target in passed):
                        mechanical[key].append(issue.format())
        return result, mechanical

    initial, mechanical = await assess(candidates, True)
    issues = {}
    for verdict in initial.decisions:
        problem = verdict_problem(verdict)
        problems = [problem, *mechanical[verdict.candidate]]
        problem = "；".join(p for p in problems if p)
        if not problem:
            passed[verdict.candidate] = candidates[verdict.candidate]
        else:
            issues[verdict.candidate] = problem
    for key, reason in enumerate(initial.missing, len(candidates) + 1):
        issues[key] = reason

    def record_progress(round_number):
        record(round_number, "concept_review_progress", {
            "accepted": sorted(passed),
            "accepted_concepts": {str(key): value.model_dump() for key, value in passed.items()},
            "unresolved": dict(issues),
        })

    record_progress(0)
    for round_number in range(1, 4):
        if not issues:
            break
        for key, reason in list(issues.items()):
            old = candidates.get(key)
            update = await agent.correct_concept(call, {**context, "problem": reason,
                "candidate": old.model_dump() if old else None})
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
            if problem:
                issues[key] = problem
            else:
                passed[key] = update.concept
                del issues[key]
            record(round_number, "concept_issue_resolved", {
                "candidate": key, "problem": reason,
                "previous_candidate": old.model_dump() if old else None,
                "candidate_after": update.concept.model_dump(), "changed": changed,
                "resolution": resolution if not problem else "unresolved",
                "review_resolution": resolution, "reason": verdict.resolution_reason,
                "remaining_problem": problem,
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
