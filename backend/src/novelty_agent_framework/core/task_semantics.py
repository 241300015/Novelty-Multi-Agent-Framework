"""Source-first task semantics, frozen before candidate generation."""
import hashlib
import json
from typing import Literal

from pydantic import Field

from ..schemas.domain import StrictModel
from .concept_evidence import build_catalog, resolve_evidence


REGISTERED_ACTIONS = frozenset((
    'uses', 'removes', 'retains', 'combines', 'targets', 'produces', 'context',
    'models', 'learns', 'trains', 'compresses', 'inputs',
))
LEGACY_NON_MECHANISMS = frozenset(('targets', 'produces', 'context'))
FactKind = Literal['mechanism', 'goal', 'outcome', 'background']
FactImportance = Literal['defining', 'supporting', 'context']


class SemanticOutputError(ValueError):
    """A model operation used up its output repair allowance."""


class TechnicalFact(StrictModel):
    subject: str = Field(min_length=1)
    action: str = Field(min_length=1, max_length=120)
    object: str = Field(min_length=1)
    statement: str = Field(min_length=1)
    importance: FactImportance
    evidence_ids: list[str] = Field(min_length=1)
    kind: FactKind | None = None


class SemanticFacts(StrictModel):
    research_goal: str = Field(min_length=1)
    facts: list[TechnicalFact] = Field(min_length=1, max_length=16)


class ActionDecision(StrictModel):
    decision: Literal['accept', 'revise', 'reject', 'uncertain']
    reason: str = Field(min_length=1)
    source_supported: bool
    relation_preserved: bool
    qualifiers_preserved: bool
    kind: FactKind | None = None
    importance: FactImportance | None = None


class ActionReview(StrictModel):
    decisions: dict[str, ActionDecision]


class ActionCorrection(StrictModel):
    facts: dict[str, TechnicalFact]


class FactRequirement(StrictModel):
    any_of: list[str] = Field(min_length=1)
    reason: str = Field(min_length=1)


class LevelRequirements(StrictModel):
    requirements: list[FactRequirement] = Field(min_length=1, max_length=16)
    relaxation_reason: str = Field(min_length=1)


class SemanticPolicy(StrictModel):
    strict: LevelRequirements
    medium: LevelRequirements
    broad: LevelRequirements


def model_source_view(payload):
    """Hide integrity hashes from selectors; the full catalog stays program-owned."""
    if 'source_catalog' not in payload:
        return payload
    return {**payload,'source_catalog':{'sources':[
        {key:source[key] for key in ('source_id','text','field','language')}
        for source in payload['source_catalog']['sources']]}}


def validate_main_terms(concepts):
    for index,concept in enumerate(concepts,1):
        if len(concept.terms)!=1:
            raise ValueError(f'semantics.canonical: candidate {index} must contain one main term; put equivalent variants in alias and split different mechanisms into separate candidates')


def action_code(action):
    normalized = ' '.join(action.casefold().split())
    return normalized if normalized in REGISTERED_ACTIONS else None


def normalize_outcome_importance(raw, operation):
    """Resolve the observed category echo only when the separate kind agrees."""
    if not isinstance(raw, dict) or operation != 'facts':
        return []
    facts = raw.get('facts')
    if not isinstance(facts, list):
        return []
    repaired = []
    for index, fact in enumerate(facts, 1):
        if isinstance(fact, dict) and fact.get('kind') == fact.get('importance') == 'outcome':
            fact['importance'] = 'context'
            repaired.append(f'F{index}')
    return repaired


def is_defining_mechanism(fact):
    if fact['importance'] != 'defining':
        return False
    if fact.get('kind') is not None:
        return fact['kind'] == 'mechanism'
    # Legacy facts predate the separate kind field. Unknown actions never use
    # this fallback to qualify as mechanisms.
    code = action_code(fact['action'])
    return code is not None and code not in LEGACY_NON_MECHANISMS


def validate_fact(fact, point, task, catalog):
    if not all(v.strip() for v in (fact.subject, fact.action, fact.object, fact.statement)):
        raise ValueError('semantics.fact: nonblank subject/action/object/statement required')
    resolve_evidence(catalog, fact.evidence_ids, point, task)


def validate_facts(result, point, task, catalog):
    if not result.research_goal.strip():
        raise ValueError('semantics.goal: nonblank research goal required')
    for fact in result.facts:
        validate_fact(fact, point, task, catalog)


def validate_action_output(operation, result, expected_facts, point, task, catalog):
    returned = result.facts if operation == 'action_correct' else result.decisions
    if returned.keys() != expected_facts.keys():
        raise ValueError('semantics.action_scope: return exactly the issued fact IDs')
    if operation == 'action_correct':
        for fact in result.facts.values():
            validate_fact(fact, point, task, catalog)
    else:
        for decision in result.decisions.values():
            if decision.decision == 'accept' and (
                not all((decision.source_supported, decision.relation_preserved, decision.qualifiers_preserved))
                or decision.kind is None or decision.importance is None
            ):
                raise ValueError('semantics.action_accept: acceptance requires all checks and a fact classification')


def serialize_fact(fact, status):
    data = fact.model_dump()
    if fact.kind in ('goal', 'outcome', 'background') and fact.importance == 'defining':
        data['importance'] = 'context'
    return {**data, 'action_code': action_code(fact.action), 'action_status': status}


async def resolve_actions(extracted, source, point, task, call, record):
    issued = {f'F{i}': fact for i, fact in enumerate(extracted.facts, 1)}
    pending = {fid: fact for fid, fact in issued.items() if action_code(fact.action) is None}
    accepted = {fid: serialize_fact(fact, 'registered') for fid, fact in issued.items() if fid not in pending}
    if not pending:
        return accepted

    def payload(targets):
        return {**source, 'facts_to_review': {fid: fact.model_dump() for fid, fact in targets.items()}}

    async def review(targets, round_number):
        review_payload = payload(targets)
        if round_number == 2:
            review_payload['original_facts'] = {fid: pending[fid].model_dump() for fid in targets}
        try:
            result = await call('action_review', ActionReview, review_payload)
        except SemanticOutputError:
            return {
                fid: ActionDecision(decision='uncertain', reason='动作复核响应无法通过契约校验',
                                    source_supported=False, relation_preserved=False, qualifiers_preserved=False)
                for fid in targets
            }
        validate_action_output('action_review', result, targets, point, task, source['source_catalog'])
        record(round_number, 'semantic_action_reviewed', result.model_dump())
        return result.decisions

    record(0, 'semantic_action_pending', {'fact_ids': list(pending)})
    decisions = await review(pending, 1)
    revisable = {fid: pending[fid] for fid, decision in decisions.items() if decision.decision == 'revise'}
    evaluated = dict(pending)
    if revisable:
        try:
            correction = await call('action_correct', ActionCorrection, {
                **payload(revisable), 'review_reasons': {fid: decisions[fid].reason for fid in revisable},
            })
        except SemanticOutputError:
            record(1, 'semantic_action_correction_failed', {'fact_ids': list(revisable)})
        else:
            validate_action_output('action_correct', correction, revisable, point, task, source['source_catalog'])
            changed = {fid: fact for fid, fact in correction.facts.items() if fact != revisable[fid]}
            record(1, 'semantic_action_corrected', {'changed_ids': list(changed), 'facts': correction.model_dump()['facts']})
            if changed:
                evaluated.update(changed)
                decisions.update(await review(changed, 2))

    rejected = {}
    for fid, decision in decisions.items():
        if decision.decision != 'accept':
            rejected[fid] = {
                'reason': decision.reason, 'original_importance': pending[fid].importance,
                'decision': decision.decision,
            }
            continue
        reviewed = evaluated[fid].model_copy(update={'kind': decision.kind, 'importance': decision.importance})
        accepted[fid] = serialize_fact(reviewed, 'confirmed')
    if rejected:
        record(0, 'semantic_action_excluded', {'facts': rejected})
        if any(row['original_importance'] == 'defining' for row in rejected.values()):
            raise ValueError('semantics.action_unresolved: defining fact failed action review')
    return {fid: accepted[fid] for fid in issued if fid in accepted}


def validate_policy(result, facts):
    known = set(facts)
    defining = {key for key, value in facts.items() if is_defining_mechanism(value)}
    for level in ('strict','medium','broad'):
        policy = getattr(result, level)
        if not policy.relaxation_reason.strip():
            raise ValueError('semantics.policy: nonblank relaxation reason required')
        for group in policy.requirements:
            if len(group.any_of)!=len(set(group.any_of)) or not set(group.any_of)<=known or not group.reason.strip():
                raise ValueError(f'semantics.policy: {level} must reference issued fact IDs exactly')
        if not any(set(group.any_of)<=defining for group in policy.requirements):
            raise ValueError(f'semantics.policy: {level} requires at least one discriminative mechanism group')
        if level!='strict' and any(all(facts[f]['importance']=='supporting' for f in g.any_of) for g in policy.requirements):
            raise ValueError(f'semantics.relaxation: {level} cannot make an optional supporting detail mandatory; use defining mechanism groups')
    strict_singletons = {g.any_of[0] for g in result.strict.requirements if len(g.any_of)==1}
    if not defining <= strict_singletons:
        raise ValueError('semantics.strict: each defining mechanism must be required in strict')


async def build_task_contract(point, task, call, record):
    catalog = build_catalog(point,task)
    record(0,'concept_source_catalog',catalog)
    source = dict(point=point.model_dump(),task=task.model_dump(),source_catalog=catalog)
    extracted = await call('facts',SemanticFacts,source)
    validate_facts(extracted,point,task,catalog)
    facts = await resolve_actions(extracted, source, point, task, call, record)
    if not any(is_defining_mechanism(fact) for fact in facts.values()):
        raise ValueError('semantics.defining: require a confirmed discriminative technical mechanism')
    policy = await call('policy',SemanticPolicy,{**source,'research_goal':extracted.research_goal,'facts':facts})
    validate_policy(policy,facts)
    contract = dict(version=1,point_id=point.point_id,task_id=task.task_id,
        input_sha256=hashlib.sha256(json.dumps(point.model_dump(),sort_keys=True,ensure_ascii=False).encode()).hexdigest(),
        research_goal=extracted.research_goal,facts=facts,levels=policy.model_dump(),origin='automatic_source_first',
        assessment='machine_generated_not_per_paper_human_approval')
    contract['contract_id'] = hashlib.sha256(json.dumps(contract,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
    record(0,'task_semantic_contract',contract)
    return contract


def validate_contract_binding(contract, point, task):
    if (contract['point_id'],contract['task_id'])!=(point.point_id,task.task_id):
        raise ValueError('semantics.binding: contract belongs to another point/task')
    expected = hashlib.sha256(json.dumps(point.model_dump(),sort_keys=True,ensure_ascii=False).encode()).hexdigest()
    if contract['input_sha256']!=expected:
        raise ValueError('semantics.binding: original input changed after contract creation')
    body = {k:v for k,v in contract.items() if k!='contract_id'}
    if hashlib.sha256(json.dumps(body,sort_keys=True,ensure_ascii=False).encode()).hexdigest()!=contract['contract_id']:
        raise ValueError('semantics.integrity: frozen contract changed')


def missing_requirements(expression, concept_fact_links, contract, level):
    """Every OR branch must meet every required fact group; never trust a pass word."""
    from .search_plan_expression import parse_search_plan_expression
    tokens = parse_search_plan_expression(expression,defined_concepts=set(concept_fact_links))
    position = 0
    def unique(branches):
        result = set()
        for branch in branches:
            result.add(frozenset(branch))
            if len(result)>64:
                raise ValueError('semantics.expression: too many alternative branches; simplify expression')
        return list(result)
    def operand():
        nonlocal position
        token=tokens[position]
        position+=1
        if token=='(':
            branches=disjunction()
            position+=1  # parser has already validated matching parentheses
            return branches
        return [frozenset(concept_fact_links[token])]
    def conjunction():
        nonlocal position
        result=operand()
        while position<len(tokens) and tokens[position]=='AND':
            position+=1
            right=operand()
            result=unique(left|r for left in result for r in right)
        return result
    def disjunction():
        nonlocal position
        result=conjunction()
        while position<len(tokens) and tokens[position]=='OR':
            position+=1
            result=unique([*result,*conjunction()])
        return result
    branches=disjunction()
    failures=[]
    for group in contract['levels'][level]['requirements']:
        if any(not set(group['any_of']) & branch for branch in branches):
            failures.append(group)
    return failures


def validate_strategy_judgment(verdict,payload):
    if not payload.get('semantic_contract'):
        return
    known={c['concept_id'] for c in payload['concepts']}
    if not set(verdict.fact_mapping_errors)<=known or any(not reason.strip() for reason in verdict.fact_mapping_errors.values()):
        raise ValueError('review.mapping: cite issued concept IDs and concrete mapping errors')
    if not set(verdict.contract_errors)<=set(payload['semantic_contract']['facts'])|{'source_gap'} or any(not reason.strip() for reason in verdict.contract_errors.values()):
        raise ValueError('review.contract: cite an issued fact ID or source_gap with a concrete standards error')
    if payload.get('required_groups_satisfied') and (not verdict.mechanism_preserved or verdict.missing_mechanisms) and not (verdict.fact_mapping_errors or verdict.contract_errors):
        raise ValueError('review.criteria: all frozen required fact groups are satisfied. Do not make an optional fact mandatory. If a concept does not actually express its mapped fact, cite the concept ID and error in fact_mapping_errors; otherwise judge the candidate by the frozen level requirements.')
    if verdict.decision=='revise' and all((verdict.concept_use_correct,verdict.level_appropriate,verdict.adjustment_reasonable)) and not(verdict.fact_mapping_errors or verdict.contract_errors):
        raise ValueError('review.consistency: revise requires a concrete failed dimension or mapping/contract error; use uncertain when genuinely undecided')
