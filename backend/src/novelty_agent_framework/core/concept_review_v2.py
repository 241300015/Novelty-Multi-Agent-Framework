"""Evidence-ID review boundary; retain the established correction state machine."""
from typing import Literal

from pydantic import Field

from ..schemas.domain import StrictModel
from ..schemas import NoveltyPoint, ResearchTask
from .concept_evidence import resolve_evidence
from .concept_stage import Review, Verdict, TermReview, AliasReview


class TermDecision(StrictModel):
    term_id: str
    substitutable: bool
    reason: str = Field(min_length=1)


class AliasDecision(StrictModel):
    alias_id: str
    substitutable: bool
    reason: str = Field(min_length=1)


class EvidenceVerdict(StrictModel):
    candidate_id: str
    support_status: Literal['supported', 'unsupported', 'uncertain']
    evidence_ids: list[str]
    source_reason: str = Field(min_length=1)
    role_correct: bool
    role_reason: str = Field(min_length=1)
    translation_consistent: bool | None
    translation_reason: str
    terms: list[TermDecision]
    aliases: list[AliasDecision]
    issue_resolution: Literal['fixed', 'invalid', 'unresolved'] | None = None
    resolution_reason: str = ''
    issue_ids: list[str] = Field(default_factory=list)
    fact_ids: list[str] = Field(default_factory=list)


class EvidenceReview(StrictModel):
    decisions: list[EvidenceVerdict]
    missing: list[str]


class LocalEvidenceReview(EvidenceReview):
    """Separate operation/Prompt for review of one scoped semantic correction."""


def review_payload(payload):
    candidates = {}
    for key, value in payload['candidates'].items():
        cid = f'C{key}'
        candidates[cid] = dict(role=value['role'], importance=value['importance'],
            terms={f'{cid}.T{i}': term for i, term in enumerate(value['terms'], 1)},
            aliases={f'{cid}.A{i}': term for i, term in enumerate(value['alias'], 1)})
    return {**payload, 'candidates': candidates}


def validate_evidence_review(result, payload):
    candidates = payload['candidates']
    if sorted(d.candidate_id for d in result.decisions) != sorted(candidates):
        raise ValueError(f'review.candidates: expected exactly {list(candidates)}')
    if not payload['check_coverage'] and result.missing:
        raise ValueError('review.scope: local review cannot add missing candidates')
    if payload.get('contract_repair') and result.missing:
        raise ValueError('review.scope: contract repair cannot change coverage findings')
    point = NoveltyPoint.model_validate(payload['point'])
    task = ResearchTask.model_validate(payload['task'])
    for d in result.decisions:
        value = candidates[d.candidate_id]
        for field, actual in [('terms', [x.term_id for x in d.terms]),
                              ('aliases', [x.alias_id for x in d.aliases])]:
            if sorted(actual) != sorted(value[field]):
                raise ValueError(f'review.field: {d.candidate_id}.{field} expected exactly {list(value[field])}')
        resolve_evidence(payload['source_catalog'], d.evidence_ids, point, task)
        if payload.get('semantic_contract'):
            known=payload['semantic_contract']['facts']
            if len(d.fact_ids)!=len(set(d.fact_ids)) or not set(d.fact_ids)<=set(known):
                raise ValueError(f'review.facts: {d.candidate_id} must reference issued fact IDs')
        if d.support_status == 'supported' and not d.evidence_ids:
            raise ValueError(f'evidence.required: {d.candidate_id} supported requires evidence_ids')
        if not d.source_reason.strip() or not d.role_reason.strip():
            raise ValueError(f'review.reason: {d.candidate_id} requires nonblank reasons')
        if payload['comparison']:
            if d.translation_consistent is None or not d.translation_reason.strip():
                raise ValueError(f'review.translation: {d.candidate_id} requires judgment and reason')
        elif d.translation_consistent is not None:
            raise ValueError('review.translation: no comparison requires null')
        if not payload['check_coverage'] and (d.issue_resolution is None or not d.resolution_reason.strip()):
            raise ValueError('review.resolution: local review requires resolution and reason')
        if not payload['check_coverage'] and payload.get('issues'):
            if sorted(d.issue_ids) != sorted(i['issue_id'] for i in payload['issues']):
                raise ValueError('review.issues: must address exactly the issued issue_ids')


def to_internal_review(result, payload):
    """Translate issued IDs to existing internal judgments, never trust model quotes."""
    validate_evidence_review(result, payload)
    sources = {x['source_id']: x for x in payload['source_catalog']['sources']}
    decisions = []
    for d in result.decisions:
        c = payload['candidates'][d.candidate_id]
        decisions.append(Verdict(candidate=int(d.candidate_id[1:]),
            source_supported=d.support_status == 'supported',
            source_quote=sources[d.evidence_ids[0]]['text'] if d.evidence_ids else '',
            source_reason=d.source_reason, role_correct=d.role_correct, role_reason=d.role_reason,
            translation_consistent=d.translation_consistent, translation_reason=d.translation_reason,
            terms=[TermReview(term=c['terms'][t.term_id], substitutable=t.substitutable, reason=t.reason) for t in d.terms],
            aliases=[AliasReview(alias=c['aliases'][a.alias_id], substitutable=a.substitutable, reason=a.reason) for a in d.aliases],
            issue_resolution=d.issue_resolution, resolution_reason=d.resolution_reason, fact_ids=d.fact_ids))
    return Review(decisions=decisions, missing=result.missing)
