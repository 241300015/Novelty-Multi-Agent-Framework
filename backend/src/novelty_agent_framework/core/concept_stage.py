"""Task-local concept review and bounded correction."""
from dataclasses import asdict
from typing import Literal

from pydantic import Field

from ..schemas.domain import StrictModel
from ..schemas.search_plan_draft import SearchConceptDraft


class ConceptBatch(StrictModel):
    concepts: list[SearchConceptDraft] = Field(min_length=1)
    comparison: list[SearchConceptDraft] = Field(default_factory=list)


class Verdict(StrictModel):
    candidate: int
    decision: Literal["pass", "revise"]
    terms_substitutable: bool
    aliases_belong: bool
    source_supported: bool
    reason: str = Field(min_length=1)

    @property
    def accepted(self):
        return self.decision == "pass" and self.terms_substitutable and self.aliases_belong and self.source_supported


class Review(StrictModel):
    decisions: list[Verdict]
    missing: list[str]


class Correction(StrictModel):
    concept: SearchConceptDraft


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


    async def assess(items, whole):

        result = await agent.review_concepts(call, {**context, "check_coverage": whole,
            "candidates": {str(k): v.model_dump() for k, v in items.items()}})
        ids = [v.candidate for v in result.decisions]
        if sorted(ids) != sorted(items) or (not whole and result.missing):
            raise ValueError("复核未逐项覆盖候选，或局部复核越界")
        catalog = items if whole else {**passed, **items}
        keys = list(catalog)
        for issue in validate_concept_semantics(list(catalog.values()), task=task, limits=agent.semantic_limits):
            for path in issue.paths:
                if len(path) < 2:
                    if issue.code == "missing_escape" and whole:
                        result.missing.append(issue.fix)
                        continue
                    raise ValueError(issue.detail)
                target = keys[path[1]]
                for verdict in result.decisions:
                    if verdict.candidate == target or (not whole and target in passed):
                        verdict.decision = "revise"
                        verdict.reason += "；" + issue.format()
        return result

    initial = await assess(candidates, True)
    passed = {}
    issues = {}
    for v in initial.decisions:
        if v.accepted:
            passed[v.candidate] = candidates[v.candidate]
        else:
            issues[v.candidate] = v.reason
    for key, reason in enumerate(initial.missing, len(candidates) + 1):
        issues[key] = reason
    for round_number in range(1, 4):
        if not issues:
            break
        for key, reason in list(issues.items()):
            old = candidates.get(key)
            update = await agent.correct_concept(call, {**context, "problem": reason,
                "candidate": old.model_dump() if old else None})
            candidates[key] = update.concept
            verdict = (await assess({key: update.concept}, False)).decisions[0]
            if verdict.accepted:
                passed[key] = update.concept
                del issues[key]
            else:
                issues[key] = verdict.reason
        record(round_number, "concept_review_progress", {"accepted": sorted(passed), "issues": dict(issues)})
    record(0, "concept_stage_finished", {"accepted": sorted(passed), "unresolved": dict(issues)})
    if not passed:
        raise ValueError("概念复核通过集为空")
    return [passed[key].model_dump() for key in sorted(passed)]
