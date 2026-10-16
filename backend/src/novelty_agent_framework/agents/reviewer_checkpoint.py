"""Explicit Reviewer summary recovery from immutable, source-verified card results."""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import uuid
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from pydantic import Field

from ..persistence import ReferenceStore, _atomic_write_json, paper_workspace, reference_store_for_artifact_namespace
from ..schemas import NoveltyPointReview, NoveltyPointReviewRequest, StrictModel
from ..schemas.failures import FailureCode, FailureEvent, FailureScope, make_failure
from ..core.integrity_gates import validate_card_integrity


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


class ReviewSummaryAttempt(StrictModel):
    checkpoint_id: str | None = None
    attempt_id: str
    status: Literal['completed', 'failed', 'checkpoint_unavailable']
    durable: bool = False
    review: NoveltyPointReview
    partial_card_results: list[dict[str, Any]] = Field(default_factory=list)
    execution_issues: list[FailureEvent] = Field(default_factory=list)


def _rows(request, rows):
    from .evidence_reviewer import _compact_summary_rows
    rows = json.loads(canonical(rows))
    cards = [card.card_id for card in request.cards]
    indexed = [row.get('card_id') for row in rows]
    if len(indexed) != len(set(indexed)) or set(indexed) != set(cards):
        raise ValueError('summary checkpoint requires exactly one row for every request Card')
    if any(row.get('status') not in {'completed', 'failed'} for row in rows):
        raise ValueError('summary checkpoint contains unfinished Card rows')
    _compact_summary_rows(request, rows)  # Original binding/reference checks.
    return rows


def _source_snapshot(request, rows, output_root):
    """No network or agent Reader call; verify local files and exact historical reads."""
    gate = validate_card_integrity(request.cards, evidence=request.evidence,
        tasks=request.tasks, novelty_points=[request.novelty_point],
        paper_id=request.subject_paper_id, reference_store=ReferenceStore(output_root))
    if gate.rejected:
        raise ValueError('summary checkpoint original Evidence failed synthesis provenance gate')
    snapshot = {}

    def source(namespace, artifact_id, work_id):
        store = reference_store_for_artifact_namespace(namespace, output_root=output_root)
        artifact, _, raw = store.verify_artifact_file(request.subject_paper_id, artifact_id)
        if artifact.work_id != work_id:
            raise ValueError('summary checkpoint Artifact Work changed')
        manifest = store.load_manifest(request.subject_paper_id)
        work = next(item for item in manifest.works if item.work_id == work_id)
        record = next((item for item in manifest.source_records if item.source_record_id == artifact.source_record_id), None)
        snapshot[f'{namespace}:{artifact_id}'] = {
            'artifact': artifact.model_dump(mode='json'), 'work': work.model_dump(mode='json'),
            'source_record': record.model_dump(mode='json') if record else None,
        }
        return store, artifact, raw.decode('utf-8')

    for evidence in request.evidence:
        provenance = evidence.provenance
        namespace = provenance.get('artifact_namespace', 'research_reference')
        store, artifact, text = source(namespace, evidence.artifact_id, evidence.work_id)
        start, end = provenance.get('read_char_start'), provenance.get('read_char_end')
        if not isinstance(start, int) or not isinstance(end, int) or not 0 <= start < end <= len(text):
            raise ValueError('summary checkpoint original Evidence lacks verifiable Reader range')
        read = store.read_document_slice(request.subject_paper_id, artifact_id=artifact.artifact_id,
                                         char_start=start, max_chars=end-start)
        if read.read_id != provenance.get('read_id'):
            raise ValueError('summary checkpoint original Evidence read_id mismatch')
        locator = evidence.locator
        if locator is None or locator.char_start is None or locator.char_end is None \
                or not start <= locator.char_start < locator.char_end <= end:
            raise ValueError('summary checkpoint original Evidence lacks a bound quote range')

    for row in rows:
        if row['status'] != 'completed':
            continue
        review = NoveltyPointReview.model_validate(row['review'])
        audits = {item.read_id: item for item in review.reader_observations}
        if len(audits) != len(review.reader_observations):
            raise ValueError('duplicate Reviewer Reader audit')
        # Revalidate all originally registered observations, including empty EOF.
        reads = {}
        for audit in audits.values():
            store, artifact, text = source(audit.namespace, audit.artifact_id, audit.work_id)
            current = store.read_document_slice(request.subject_paper_id, artifact_id=audit.artifact_id,
                char_start=audit.char_start, max_chars=max(1, audit.char_end-audit.char_start))
            if (current.read_id != audit.read_id or current.char_end != audit.char_end
                    or current.sha256 != audit.artifact_hash
                    or hashlib.sha256(current.text.encode()).hexdigest() != audit.text_sha256):
                raise ValueError('Reviewer Reader observation changed')
            reads[audit.read_id] = current
        for item in review.review_evidence:
            read = reads.get(item.read_id)
            if read is None:
                raise ValueError('incremental Evidence lacks original Reviewer Reader observation')
            if (item.artifact_id != read.artifact_id or item.namespace != read.namespace.value
                    or item.work_id != read.work_id or item.artifact_hash != read.sha256
                    or not read.char_start <= item.char_start < item.char_end <= read.char_end
                    or read.text[item.char_start-read.char_start:item.char_end-read.char_start] != item.exact_quote):
                raise ValueError('incremental Evidence differs from original Reader slice')
            expected = 'rev_ev_' + hashlib.sha256(
                f'{item.origin_card_id}\x1f{item.read_id}\x1f{item.char_start}\x1f{item.char_end}'.encode()).hexdigest()[:24]
            if expected != item.evidence_id:
                raise ValueError('incremental Evidence registration ID mismatch')
    return snapshot


def _configuration(reviewer):
    options = reviewer.model_options
    selected = {key: getattr(options, key, None) for key in
                ('temperature', 'max_tokens', 'timeout_seconds', 'response_format', 'tool_choice')} if options else {}
    profile = getattr(reviewer._client(), 'profile', None)
    identity = ({key:getattr(profile, key, None) for key in ('alias','provider','model','context_window')}
                if profile else {'client_type':type(reviewer._client()).__qualname__})
    if profile:
        identity['endpoint_sha256'] = hashlib.sha256(profile.base_url.encode()).hexdigest()
        identity['defaults_sha256'] = digest(profile.defaults)
    return {'reviewer': asdict(reviewer.config), 'model_options': selected, 'model_identity':identity,
            'summary_prompt_sha256': hashlib.sha256(reviewer._summary_checkpoint_prompt().encode()).hexdigest()}


class ReviewerSummaryCheckpoint:
    def __init__(self, request, *, output_root, run_id, checkpoint_id):
        if not run_id or not re.fullmatch('[0-9a-f]{32}', checkpoint_id):
            raise ValueError('explicit run_id and UUID checkpoint_id are required')
        self.request = NoveltyPointReviewRequest.model_validate(request).model_copy(deep=True)
        self.output_root, self.run_id, self.checkpoint_id = Path(output_root), run_id, checkpoint_id
        identity = {'request': self.request.model_dump(mode='json'), 'run_id': run_id}
        self.identity = identity
        self.directory = paper_workspace(request.subject_paper_id, output_root=output_root) / 'reviewer-checkpoints' / digest(identity) / checkpoint_id
        self.path = self.directory / 'checkpoint.json'

    @classmethod
    def create(cls, reviewer, request, rows, *, output_root, run_id):
        session = cls(request, output_root=output_root, run_id=run_id, checkpoint_id=uuid.uuid4().hex)
        rows = _rows(session.request, rows)
        sources = _source_snapshot(session.request, rows, output_root)
        payload = {'schema_version': 1, 'checkpoint_id': session.checkpoint_id,
            **session.identity, 'card_results': rows, 'source_snapshot': sources,
            'configuration': _configuration(reviewer),
            'summary_input_date': reviewer.config.summary_input_date or datetime.now(timezone.utc).date().isoformat(),
            'max_recovery_attempts': 1}
        payload['content_sha256'] = digest(payload)
        session.directory.mkdir(parents=True, exist_ok=False)
        _atomic_write_json(session.path, payload)
        session.payload = payload
        return session

    @classmethod
    def load(cls, reviewer, request, *, output_root, run_id, checkpoint_id):
        session = cls(request, output_root=output_root, run_id=run_id, checkpoint_id=checkpoint_id)
        payload = json.loads(session.path.read_text(encoding='utf-8'))
        checksum = payload.pop('content_sha256', None)
        if checksum != digest(payload) or payload.get('schema_version') != 1:
            raise ValueError('summary checkpoint content hash/schema mismatch')
        if payload.get('checkpoint_id') != checkpoint_id or any(payload.get(key) != value for key,value in session.identity.items()):
            raise ValueError('summary checkpoint request/run scope mismatch')
        if payload.get('configuration') != _configuration(reviewer):
            raise ValueError('summary checkpoint model/prompt/configuration changed; explicit new experiment required')
        rows = _rows(session.request, payload['card_results'])
        if _source_snapshot(session.request, rows, output_root) != payload['source_snapshot']:
            raise ValueError('summary checkpoint source metadata changed')
        payload['content_sha256'] = checksum
        session.payload = payload
        return session

    async def attempt(self, reviewer, *, recovery=False):
        # Exclusive directory reservation prevents concurrent retries and snapshot overwrite.
        attempt_id = 'recovery-1' if recovery else 'initial'
        prior = self.directory / 'initial' / 'result.json'
        if recovery and prior.exists() and json.loads(prior.read_text()).get('status') == 'completed':
            raise ValueError('summary already completed; recovery cannot resample a valid result')
        attempt_dir = self.directory / attempt_id
        attempt_dir.mkdir(exist_ok=False)
        rows = self.payload['card_results']
        _atomic_write_json(attempt_dir / 'state.json', {'status':'running',
            'checkpoint_sha256':self.payload['content_sha256'], 'attempt_id':attempt_id})
        try:
            review = await reviewer.summarize_reviews(self.request, rows,
                _input_date=self.payload['summary_input_date'])
        except BaseException as exc:
            # Cancellation must propagate; recording it cannot erase card results.
            try:
                _atomic_write_json(attempt_dir / 'state.json', {'status':'cancelled' if isinstance(exc, asyncio.CancelledError) else 'failed',
                    'error_type':type(exc).__name__, 'checkpoint_sha256':self.payload['content_sha256'],
                    'attempt_id':attempt_id})
            except OSError:
                pass
            raise
        failed = review.incomplete_reason in {'technical_error', 'budget_exhausted'}
        result = ReviewSummaryAttempt(checkpoint_id=self.checkpoint_id, attempt_id=attempt_id,
            status='failed' if failed else 'completed', durable=True, review=review,
            partial_card_results=rows, execution_issues=review.execution_issues)
        try:
            _atomic_write_json(attempt_dir / 'result.json', result.model_dump(mode='json'))
            _atomic_write_json(attempt_dir / 'state.json', {'status':result.status,
                'checkpoint_sha256':self.payload['content_sha256'], 'attempt_id':attempt_id})
        except OSError:
            issue = make_failure(FailureCode.REVIEW_SUMMARY_FAILED,
                scope=FailureScope(paper_id=self.request.subject_paper_id, run_id=self.run_id,
                                   point_id=self.request.novelty_point.point_id),
                message='Summary attempt result persistence failed; original checkpoint remains')
            result = result.model_copy(update={'durable':False,
                'execution_issues':[*result.execution_issues, issue]})
        return result
