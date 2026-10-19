"""Invocation-local, Builder-validated evidence checkpoints; never final verdicts."""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from typing import Any

from pydantic import Field

from ..persistence import _atomic_write_json, paper_workspace, reference_store_for_artifact_namespace
from ..schemas import (EvidenceCardBuilderResult, EvidenceCardDraft, ReferenceReadResult,
                       ResearchFinishDraft, ResearcherToolObservation, StrictModel,
                       TaskResearchRequest, TaskResearchResult, TaskResearchStatus)
from .evidence_card_builder import EvidenceCardBuilder
from .researcher_registry import ResearcherToolRegistry


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _read_key(read: ReferenceReadResult) -> tuple:
    return read.namespace, read.artifact_id, read.char_start, read.char_end, read.sha256


class EvidenceCheckpointArguments(StrictModel):
    """Submit semantic drafts only; all provenance and scope stay runtime-owned."""
    cards: list[EvidenceCardDraft] = Field(min_length=1, max_length=16)


class EvidenceCheckpointSession:
    """Owned by one ainvoke; explicit disk recovery requires the identical request."""

    def __init__(self, builder: EvidenceCardBuilder, request: TaskResearchRequest, *,
                 invocation_id: str | None = None) -> None:
        self.builder = builder
        self.request = TaskResearchRequest.model_validate(request).model_copy(deep=True)
        self.invocation_id = invocation_id or uuid.uuid4().hex
        if not re.fullmatch(r"[a-f0-9]{32}", self.invocation_id):
            raise ValueError("checkpoint invocation_id must be a runtime-generated UUID hex")
        self._scope = self.request.model_dump(mode="json")
        scope_hash = hashlib.sha256(_canonical(self._scope).encode()).hexdigest()
        self.path = (paper_workspace(request.subject_paper_id,
                                    output_root=builder.reference_store.output_root)
                     / "evidence-checkpoints" / scope_hash / f"{self.invocation_id}.json")
        self.reads: dict[tuple, ReferenceReadResult] = {}
        self.cards: dict[str, Any] = {}
        self.evidence: dict[str, Any] = {}
        self.drafts: dict[str, EvidenceCardDraft] = {}
        self.warnings: list[str] = []
        self.execution_status = "in_progress"
        self.failure_warnings: list[str] = []
        self.submissions = 0
        self.submission_history: list[dict[str, Any]] = []

    def assert_scope(self, request: TaskResearchRequest) -> None:
        if TaskResearchRequest.model_validate(request).model_dump(mode="json") != self._scope:
            raise ValueError("checkpoint scope differs from the bound invocation")

    def observe(self, observation: ResearcherToolObservation) -> None:
        if observation.tool_name != "reader" or not observation.succeeded:
            return
        rows = observation.payload.get("read_results", [observation.payload.get("read_result")])
        for row in rows:
            try:
                read = ReferenceReadResult.model_validate(row)
                key = _read_key(read)
                if key in self.reads and self.reads[key] != read:
                    raise ValueError("conflicting Reader observation")
                self.reads[key] = read
            except (TypeError, ValueError) as exc:
                self.warnings.append(f"checkpoint ignored malformed Reader observation: {type(exc).__name__}")

    def submit(self, arguments: EvidenceCheckpointArguments, *,
               scope: TaskResearchRequest) -> EvidenceCardBuilderResult:
        self.assert_scope(scope)
        draft = ResearchFinishDraft(cards=arguments.cards)
        built = self.builder.build(draft, scope=self.request, read_results=list(self.reads.values()))
        rejected = {item.card_index for item in built.rejections}
        accepted_drafts = [item for index, item in enumerate(draft.cards) if index not in rejected]
        if len(accepted_drafts) != len(built.evidence_cards):
            raise ValueError("Builder result cannot be bound to submitted drafts")
        for card, item in zip(built.evidence_cards, accepted_drafts, strict=True):
            self.cards[card.card_id] = card
            self.drafts[card.card_id] = item
        self.evidence.update({item.evidence_id: item for item in built.evidence})
        self.warnings.extend(built.warnings)
        self.submissions += 1
        self.submission_history.append({
            "sequence": self.submissions,
            "accepted_drafts": [item.model_dump(mode="json") for item in accepted_drafts],
            "builder_result": built.model_dump(mode="json"),
        })
        # Persist before returning a successful acknowledgement to the model.
        self._persist()
        return built

    def _needed_evidence(self) -> list:
        needed = {eid for card in self.cards.values() for eid in card.evidence_ids}
        return [item for eid, item in self.evidence.items() if eid in needed]

    def _persist(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write_json(self.path, {
            "schema_version": 1, "invocation_id": self.invocation_id,
            "scope": self._scope, "validation_stage": "builder_provenance_only",
            "execution_status": self.execution_status, "failure_warnings": self.failure_warnings,
            "submissions": self.submissions, "submission_history": self.submission_history,
            "accepted_drafts": [item.model_dump(mode="json") for item in self.drafts.values()],
            "read_results": [item.model_dump(mode="json") for item in self.reads.values()],
            "builder_result": EvidenceCardBuilderResult(evidence=self._needed_evidence(),
                evidence_cards=list(self.cards.values()), warnings=self.warnings).model_dump(mode="json"),
        })

    def merge_into(self, result: TaskResearchResult, *, persist_status: bool = True) -> TaskResearchResult:
        if result.task_id != self.request.research_task.task_id or result.novelty_point_id != self.request.novelty_point.point_id:
            raise ValueError("checkpoint result has wrong task binding")
        if not self.submissions:
            return result
        # A valid final draft can revise the same Work; it cannot erase omitted
        # checkpoints. Latest validated submission wins within a checkpoint.
        cards = {**self.cards, **{card.card_id: card for card in result.evidence_cards}}
        evidence = {**self.evidence, **{item.evidence_id: item for item in result.evidence}}
        needed = {eid for card in cards.values() for eid in card.evidence_ids}
        reads = {**self.reads, **{_read_key(read): read for read in result.read_results}}
        warnings = [*result.warnings, *self.warnings,
                    f"evidence checkpoint {self.invocation_id}: {len(self.cards)} Builder-validated card(s); downstream Validator/Reviewer still required"]
        if persist_status:
            self.execution_status = result.status.value
            self.failure_warnings = list(result.warnings) if result.status == TaskResearchStatus.PARTIAL else []
            try:
                self._persist()
            except OSError as exc:
                warnings.append(f"checkpoint final status persistence failed: {type(exc).__name__}")
        return TaskResearchResult.model_validate({**result.model_dump(mode="python"),
            "read_results": list(reads.values()), "evidence_cards": list(cards.values()),
            "evidence": [item for eid, item in evidence.items() if eid in needed],
            "warnings": list(dict.fromkeys(warnings))})

    @classmethod
    def recover(cls, builder: EvidenceCardBuilder, request: TaskResearchRequest, *,
                invocation_id: str) -> TaskResearchResult:
        session = cls(builder, request, invocation_id=invocation_id)
        payload = json.loads(session.path.read_text(encoding="utf-8"))
        if payload.get("schema_version") != 1 or payload.get("invocation_id") != invocation_id or payload.get("scope") != session._scope:
            raise ValueError("checkpoint identity/scope does not match the recovery request")
        # Never trust stored quotes/IDs merely because they were in a checkpoint.
        # Verify current manifest hash, local file and exact original Reader slice.
        from ..core.target_paper_identity import artifact_is_target
        from .reader import ReaderTool
        from .reference_reader import ReferenceArtifactReaderTool
        reader = ReaderTool(ReferenceArtifactReaderTool(builder.reference_store))
        for row in payload.get("read_results", []):
            previous = ReferenceReadResult.model_validate(row)
            if request.target_identity and artifact_is_target(
                    reader, request.subject_paper_id, previous.artifact_id, request.target_identity):
                raise ValueError("target paper artifact is excluded from checkpoint recovery")
            store = reference_store_for_artifact_namespace(previous.namespace,
                        output_root=builder.reference_store.output_root)
            current = store.read_document_slice(request.subject_paper_id,
                artifact_id=previous.artifact_id, char_start=previous.char_start,
                max_chars=max(1, previous.char_end - previous.char_start))
            if current != previous:
                raise ValueError("checkpoint Reader slice or Artifact changed")
            session.reads[_read_key(current)] = current
        drafts = [EvidenceCardDraft.model_validate(row) for row in payload.get("accepted_drafts", [])]
        built = (builder.build(ResearchFinishDraft(cards=drafts), scope=request,
                 read_results=list(session.reads.values())) if drafts else EvidenceCardBuilderResult())
        if built.rejections or len(built.evidence_cards) != len(drafts):
            raise ValueError("checkpoint no longer passes the original EvidenceCardBuilder")
        return TaskResearchResult(task_id=request.research_task.task_id,
            novelty_point_id=request.novelty_point.point_id, status=TaskResearchStatus.PARTIAL,
            read_results=list(session.reads.values()), evidence=built.evidence,
            evidence_cards=built.evidence_cards, steps_used=0,
            warnings=[*payload.get("failure_warnings", []), *built.warnings,
                f"explicitly recovered checkpoint {invocation_id}; original execution {payload.get('execution_status', 'unknown')}; no model or workflow resumed; downstream Validator/Reviewer still required"])


class EvidenceCheckpointTool:
    name = "submit_evidence"
    description = ("Checkpoint partial evidence drafts grounded in successful Reader text. "
                   "Original Builder validation applies; this does not finish the task or imply Reviewer approval.")
    args_schema = EvidenceCheckpointArguments

    def __init__(self, session: EvidenceCheckpointSession) -> None:
        self.session = session

    async def ainvoke(self, arguments: EvidenceCheckpointArguments, *, scope: TaskResearchRequest) -> ResearcherToolObservation:
        built = self.session.submit(arguments, scope=scope)
        return ResearcherToolObservation(tool_name=self.name, arguments=arguments.model_dump(mode="json"),
            succeeded=True, summary=f"checkpoint accepted {len(built.evidence_cards)} card(s)",
            payload={"checkpoint_id": self.session.invocation_id, "durable": True,
                "validation_stage": "builder_provenance_only",
                "accepted_card_ids": [card.card_id for card in built.evidence_cards],
                "accepted_evidence_ids": [item.evidence_id for item in built.evidence],
                "rejections": [item.model_dump(mode="json") for item in built.rejections],
                "total_checkpoint_cards": len(self.session.cards), "warnings": built.warnings})


class EvidenceCheckpointRegistry(ResearcherToolRegistry):
    """Fresh registry per invocation, retaining the original scope filtering gates."""
    def __init__(self, original: ResearcherToolRegistry, session: EvidenceCheckpointSession) -> None:
        super().__init__([original.get(name) for name in original.names])
        self.session = session
        self.register(EvidenceCheckpointTool(session))

    async def execute_validated(self, tool_name, arguments, *, scope, started=None):
        self.session.assert_scope(scope)
        observation = await super().execute_validated(tool_name, arguments, scope=scope, started=started)
        self.session.observe(observation)
        return observation
