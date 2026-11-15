"""Scoped recovery for issued review candidates and semantic issues."""
from copy import deepcopy
from typing import Literal

from ..schemas.domain import StrictModel
from .concept_review_v2 import EvidenceReview, validate_evidence_review


class RoleUpdate(StrictModel):
    field_path: str
    role: Literal['object', 'method', 'feature', 'setting', 'escape']


class RoleRepair(StrictModel):
    updates: list[RoleUpdate]


def apply_role_repair(raw, errors, repair):
    paths = {'.'.join(str(p) for p in e['loc']): e['loc'] for e in errors}
    if sorted(u.field_path for u in repair.updates) != sorted(paths):
        raise ValueError(f'role.scope: expected exactly {list(paths)}')
    result = deepcopy(raw)
    for update in repair.updates:
        field, index, _ = paths[update.field_path]
        result[field][index]['role'] = update.role
    return result


def split_review_repair(raw, payload):
    """Keep valid candidate judgments; return only the failed candidate inputs."""
    if not isinstance(raw, dict) or not isinstance(raw.get('decisions'), list):
        return None
    if not isinstance(raw.get('missing'), list) or any(not isinstance(x, str) for x in raw['missing']):
        return None
    if not payload['check_coverage'] and raw['missing']:
        return None
    valid = {}
    for key, value in payload['candidates'].items():
        matches = [d for d in raw['decisions'] if isinstance(d, dict) and d.get('candidate_id') == key]
        if len(matches) != 1:
            continue
        try:
            result = EvidenceReview.model_validate(dict(decisions=matches, missing=[]))
            validate_evidence_review(result, {**payload, 'candidates': {key: value}})
        except ValueError:
            continue
        valid[key] = matches[0]
    failed = {k:v for k,v in payload['candidates'].items() if k not in valid}
    if not valid or not failed:
        return None
    return valid, {**payload, 'candidates': failed, 'contract_repair': True}, raw['missing']


def semantic_issues(verdict, concept, mechanical=(), mechanical_fields=()):
    key = verdict.candidate
    issues = []
    def add(code, path, reason, fields):
        issues.append(dict(issue_id=f'I-C{key}-{code}', candidate_id=f'C{key}',
            field_path=path, code=code, reason=reason, allowed_fields=fields, status='open'))
    if not verdict.source_supported:
        add('source', ['terms'], verdict.source_reason, [])
    if not verdict.role_correct:
        add('role', ['role'], verdict.role_reason, ['role'])
    if verdict.translation_consistent is False:
        add('translation', ['terms'], verdict.translation_reason, ['terms', 'alias'])
    if len(verdict.terms) > 1 and any(not t.substitutable for t in verdict.terms):
        add('terms', ['terms'], '同组词项不能作为等义 OR 替代', ['terms'])
    for i, a in enumerate(verdict.aliases, 1):
        if not a.substitutable:
            add(f'alias-{i}', ['alias', i-1], a.reason, ['alias'])
    for i, reason in enumerate(mechanical):
        add(f'mechanical-{i}', [], reason, sorted(set(mechanical_fields)))
    return issues


def validate_scoped_correction(result, payload):
    original = payload.get('candidate')
    issues = payload.get('issues')
    if original is None or not issues:
        return
    allowed = {field for issue in issues for field in issue['allowed_fields']}
    after = result.concept.model_dump()
    changed = {key for key in original if original[key] != after[key]}
    if not changed <= allowed:
        raise ValueError(f'correction.scope: changed {sorted(changed-allowed)}; allowed {sorted(allowed)}; preserve candidate identity')
    if 'terms' in changed and not any(i['code'] == 'translation' or
            (i['code'].startswith('mechanical') and 'terms' in i['allowed_fields']) for i in issues):
        if not set(after['terms']) <= set(original['terms']):
            raise ValueError('correction.identity: OR-group repair may only retain original equivalent terms; new mechanisms require a new candidate')
