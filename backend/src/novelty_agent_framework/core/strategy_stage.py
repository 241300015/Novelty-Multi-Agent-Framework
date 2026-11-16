"""Sequential strategy generation and independent review over frozen concepts."""
from typing import Literal

from pydantic import Field

from ..schemas import SearchConcept, SearchPlan, SearchStrategy
from ..schemas.domain import StrictModel
from .search_plan_expression import parse_search_plan_expression
from ..tools.database_search.adapter import compile_search_plan


class StrategyChoice(StrictModel):
    expression: str = Field(min_length=1, max_length=2000)
    use_alias: bool = False
    use_exclude: bool = True


class StrategyVerdict(StrictModel):
    decision: Literal['pass', 'revise', 'uncertain']
    concept_use_correct: bool
    mechanism_preserved: bool
    level_appropriate: bool
    adjustment_reasonable: bool
    reason: str = Field(min_length=1)
    missing_mechanisms: list[str] = Field(default_factory=list)
    fact_mapping_errors: dict[str,str] = Field(default_factory=dict)
    contract_errors: dict[str,str] = Field(default_factory=dict)

    @property
    def accepted(self):
        return self.decision == 'pass' and all((self.concept_use_correct,
            self.mechanism_preserved, self.level_appropriate, self.adjustment_reasonable)) and not self.missing_mechanisms and not self.fact_mapping_errors and not self.contract_errors


class GroundedStrategyVerdict(StrictModel):
    """Fact coverage is program-owned; model judges meaning and policy adequacy."""
    decision: Literal['pass','revise','uncertain']
    concept_use_correct: bool
    level_appropriate: bool
    adjustment_reasonable: bool
    reason: str = Field(min_length=1)
    fact_mapping_errors: dict[str,str] = Field(default_factory=dict)
    contract_errors: dict[str,str] = Field(default_factory=dict)

    def as_verdict(self):
        return StrategyVerdict(**self.model_dump(),mechanism_preserved=not(self.fact_mapping_errors or self.contract_errors))


class StrategyContractExhausted(ValueError):
    """Current operation cannot recover a usable model output."""


class SemanticContractRejected(ValueError):
    """The automatic standard itself is disputed; never force a semantic pass."""


def compile_choice(choice, *, level, concepts, task, database):
    tokens = parse_search_plan_expression(choice.expression, defined_concepts={c.concept_id for c in concepts})
    depth = 0
    if len(tokens) > 128:
        raise ValueError('strategy.limit: at most 128 expression tokens')
    for token in tokens:
        depth += (token == '(') - (token == ')')
        if depth > 12:
            raise ValueError('strategy.limit: at most 12 nesting levels')
    strategy = SearchStrategy(strategy_id=f'S{("strict","medium","broad").index(level)+1}',
        level=level, expression=choice.expression, use_alias=choice.use_alias, use_exclude=choice.use_exclude)
    plan = SearchPlan(task_id=task.task_id, novelty_point_id=task.novelty_point_id,
                      concepts=concepts, strategies=[strategy])
    expanded = compile_search_plan(plan, database=database)[0].query
    return strategy.model_copy(update={'description': expanded}), expanded


async def run_strategy_stage(point, task, drafts, call, record, *, database='arxiv', limits=None,
                             semantic_contract=None, concept_fact_links=None):
    """First attempt + three semantic repairs per level; calls own contract budget."""
    if task.novelty_point_id != point.point_id:
        raise ValueError('Strategy task is bound to another novelty point')
    from ..agents.search_plan_compiler import validate_concept_semantics
    issues = validate_concept_semantics(drafts, task=task, limits=limits)
    if issues:
        raise ValueError('Frozen concept catalog is invalid: ' + ';'.join(i.detail for i in issues))
    concepts = [SearchConcept(concept_id=f'C{i}', name=c.terms[0], **c.model_dump()) for i,c in enumerate(drafts,1)]
    if semantic_contract:
        from .task_semantics import validate_contract_binding, missing_requirements
        validate_contract_binding(semantic_contract,point,task)
        if set(concept_fact_links or {})!={c.concept_id for c in concepts}:
            raise ValueError('semantics.mapping: frozen concepts require complete fact links')
        if any(not set(ids)<=set(semantic_contract['facts']) for ids in concept_fact_links.values()):
            raise ValueError('semantics.mapping: concept references unknown facts')
    passed = []
    failed = {}
    latest = None

    def progress(round_number):
        record(round_number, 'strategy_review_progress', dict(
            accepted_strategies=[s.model_dump() for s in passed], failed_levels=dict(failed)))

    progress(0)
    for level in ('strict','medium','broad'):
        if semantic_contract:
            available={f for ids in concept_fact_links.values() for f in ids}
            impossible=[g for g in semantic_contract['levels'][level]['requirements'] if not available & set(g['any_of'])]
            if impossible:
                failed[level]='通过概念无法覆盖本档最低事实要求：'+str([g['any_of'] for g in impossible])
                progress(0)
                continue
        feedback = ''
        previous = None
        for round_number in range(4):
            payload = dict(point=point.model_dump(), task=task.model_dump(), level=level,
                concepts=[c.model_dump() for c in concepts], previous_passed=latest,
                previous_candidate=previous, feedback=feedback, database=database)
            if semantic_contract:
                payload.update(semantic_contract=semantic_contract,concept_fact_links=concept_fact_links,
                    level_requirements=semantic_contract['levels'][level])
            try:
                choice = await call('generate', StrategyChoice, payload)
                try:
                    current, expanded = compile_choice(choice, level=level, concepts=concepts, task=task, database=database)
                except ValueError as exc:
                    # Syntax/reference failures belong to the generation contract,
                    # never to semantic-review passes or accepted strategies.
                    raise StrategyContractExhausted(str(exc)) from exc
                previous = choice.model_dump()
                if semantic_contract:
                    missing=missing_requirements(choice.expression,concept_fact_links,semantic_contract,level)
                    if missing:
                        feedback='存在可命中但不满足最低事实要求的OR分支：'+str([g['any_of'] for g in missing])
                        record(round_number,'strategy_fact_rejected',dict(level=level,expression=choice.expression,missing_requirements=missing))
                        continue
                if latest is not None and expanded == latest['expanded_query']:
                    feedback = '实际展开查询与最近通过策略完全相同，未形成新的档位调整。'
                    record(round_number, 'strategy_semantic_rejected', dict(level=level, reason=feedback, expression=choice.expression))
                    continue
                current_ids = {t for t in parse_search_plan_expression(choice.expression,
                    defined_concepts={c.concept_id for c in concepts}) if t.startswith('C')}
                previous_ids = ({t for t in parse_search_plan_expression(latest['strategy']['expression'],
                    defined_concepts={c.concept_id for c in concepts}) if t.startswith('C')} if latest else set())
                # A reviewer receives the actual candidate and deterministic
                # comparison, not the generator's repair feedback/conversation.
                review_payload = dict(point=point.model_dump(), task=task.model_dump(), level=level,
                    concepts=[c.model_dump() for c in concepts], previous_passed=latest,
                    current_strategy=current.model_dump(), expanded_query=expanded, database=database,
                    comparison=dict(identical_expanded_query=expanded==latest['expanded_query'],
                        added_concept_ids=sorted(current_ids-previous_ids),
                        removed_concept_ids=sorted(previous_ids-current_ids)) if latest else None)
                if semantic_contract:
                    review_payload.update(semantic_contract=semantic_contract,concept_fact_links=concept_fact_links,
                        level_requirements=semantic_contract['levels'][level],required_groups_satisfied=True)
                verdict = await call('review', GroundedStrategyVerdict if semantic_contract else StrategyVerdict, review_payload)
                if isinstance(verdict,GroundedStrategyVerdict):
                    verdict=verdict.as_verdict()
                record(round_number, 'strategy_semantic_review', dict(level=level, verdict=verdict.model_dump(),
                    expression=choice.expression, expanded_query=expanded))
                if verdict.contract_errors:
                    failed[level]='自动判据异常：'+str(verdict.contract_errors)
                    progress(round_number)
                    record(round_number,'semantic_contract_rejected',dict(level=level,errors=verdict.contract_errors))
                    raise SemanticContractRejected('Automatic task standard requires a new source-first assessment')
                if verdict.accepted:
                    passed.append(current)
                    latest = dict(strategy=current.model_dump(), expanded_query=expanded)
                    progress(round_number)
                    break
                feedback = verdict.reason + ('；缺失机制：' + '、'.join(verdict.missing_mechanisms) if verdict.missing_mechanisms else '')
            except StrategyContractExhausted as exc:
                feedback = str(exc)
                failed[level] = feedback
                progress(round_number)
                break
        else:
            failed[level] = feedback or '语义纠错三轮后未通过'
            progress(3)
    record(0, 'strategy_stage_finished', dict(accepted_strategies=[s.model_dump() for s in passed], failed_levels=failed))
    if not passed:
        raise ValueError('策略复核通过集为空')
    return SearchPlan(task_id=task.task_id, novelty_point_id=task.novelty_point_id,
                      concepts=concepts, strategies=passed)
