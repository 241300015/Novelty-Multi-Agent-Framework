"""Source-first task semantics, frozen before candidate generation."""
import hashlib
import json
from typing import Literal

from pydantic import Field

from ..schemas.domain import StrictModel
from .concept_evidence import build_catalog, resolve_evidence


class TechnicalFact(StrictModel):
    subject: str = Field(min_length=1)
    action: Literal['uses', 'removes', 'retains', 'combines', 'targets', 'produces', 'context']
    object: str = Field(min_length=1)
    statement: str = Field(min_length=1)
    importance: Literal['defining', 'supporting', 'context']
    evidence_ids: list[str] = Field(min_length=1)


class SemanticFacts(StrictModel):
    research_goal: str = Field(min_length=1)
    facts: list[TechnicalFact] = Field(min_length=1, max_length=16)


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


def validate_facts(result, point, task, catalog):
    if not result.research_goal.strip():
        raise ValueError('semantics.goal: nonblank research goal required')
    for fact in result.facts:
        if not all(v.strip() for v in (fact.subject, fact.object, fact.statement)):
            raise ValueError('semantics.fact: nonblank subject/object/statement required')
        resolve_evidence(catalog, fact.evidence_ids, point, task)
    if not any(f.importance == 'defining' and f.action not in ('context','targets','produces') for f in result.facts):
        raise ValueError('semantics.defining: require a discriminative technical mechanism, not only a goal or result')


def validate_policy(result, facts):
    known = set(facts)
    defining = {key for key,value in facts.items() if value['importance']=='defining'
                and value['action'] not in ('context','targets','produces')}
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
    facts = {f'F{i}':fact.model_dump() for i,fact in enumerate(extracted.facts,1)}
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
