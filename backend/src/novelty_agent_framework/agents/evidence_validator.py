"""Evidence Card 的确定性合法性与溯源门控。"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

from pydantic import ValidationError
from typing import Sequence

from ..schemas import Evidence, EvidenceCard, NoveltyPoint, ResearchTask
from ..ports import MetadataRetrieval, ValidationResult
from ..persistence import ReferenceStore


@dataclass(frozen=True)
class EvidenceValidationConfig:
    """引文和位置字段要求；不根据模型评分筛选证据。"""

    require_direct_quote: bool = True
    require_source_location: bool = True


class DefaultEvidenceValidator:
    """拒绝字段不合法、不可追溯或任务归属错误的证据。"""

    def __init__(self, config: EvidenceValidationConfig | None = None, *,
                 metadata_retrieval: MetadataRetrieval | None = None,
                 metadata_query_limit: int = 32,
                 metadata_unavailable_reason: str = "configuration_disabled",
                 metadata_startup_error_type: str | None = None,
                 metadata_startup_error_category: str | None = None,
                 metadata_startup_error_detail: str | None = None) -> None:
        self.config = config or EvidenceValidationConfig()
        if type(metadata_query_limit) is not int or metadata_query_limit < 1:
            raise ValueError("metadata_query_limit must be positive")
        self.metadata_retrieval = metadata_retrieval
        self.metadata_query_limit = metadata_query_limit
        self.metadata_unavailable_reason = metadata_unavailable_reason
        self.metadata_startup_error_type = metadata_startup_error_type
        self.metadata_startup_error_category = metadata_startup_error_category
        self.metadata_startup_error_detail = metadata_startup_error_detail

    async def avalidate(self, cards, **kwargs) -> ValidationResult:
        if self.metadata_retrieval is None:
            return self.validate(cards, **kwargs)
        import asyncio
        return await asyncio.to_thread(self.validate, cards, **kwargs)

    def validate(
        self,
        cards: Sequence[EvidenceCard],
        *,
        tasks: Sequence[ResearchTask],
        evidence: Sequence[Evidence],
        novelty_points: Sequence[NoveltyPoint],
        paper_id: str,
        reference_store: ReferenceStore,
    ) -> ValidationResult:
        """Validate Card fields and persisted provenance before any review."""
        if self.metadata_retrieval is not None:
            from ..core.metadata_verification import validate_with_external_metadata
            return validate_with_external_metadata(
                self, cards, tasks=tasks, evidence=evidence, novelty_points=novelty_points,
                paper_id=paper_id, reference_store=reference_store,
            )
        result = self._validate_local(cards, tasks=tasks, evidence=evidence,
            novelty_points=novelty_points, paper_id=paper_id, reference_store=reference_store)
        from dataclasses import replace
        from ..schemas.failures import FailureCode, FailureScope, make_failure
        reason = self.metadata_unavailable_reason
        audit = dict(result.integrity)
        audit["metadata_mode"] = "local"
        audit["metadata_unavailable_reason"] = reason
        audit["metadata_startup_error_type"] = self.metadata_startup_error_type
        audit["metadata_startup_error_category"] = self.metadata_startup_error_category
        audit["metadata_startup_error_detail"] = self.metadata_startup_error_detail
        audit["metadata_authenticity"] = {
            card.card_id: {"status": "skipped", "query_status": "not_started", "reason": reason,
                           "startup_error_type": self.metadata_startup_error_type,
                           "startup_error_category": self.metadata_startup_error_category,
                           "startup_error_detail": self.metadata_startup_error_detail,
                           "final_result": "accepted" if card in result.accepted else "rejected"}
            for card in cards
        }
        events = ()
        issues = result.issues
        if reason == "initialization_failed":
            event = make_failure(FailureCode.VALIDATOR_METADATA_INITIALIZATION,
                scope=FailureScope(node="validate_evidence", paper_id=paper_id, provider="metadata_service"),
                message=f"元数据服务初始化失败，使用本地模式；类别：{self.metadata_startup_error_category or 'initialization_exception'}；原因代码：{self.metadata_startup_error_detail or 'unspecified'}；异常类型：{self.metadata_startup_error_type}")
            events = (event,)
            issues += ((event.code.value, event.message, "warning", None),)
        return replace(result, integrity=audit, failure_events=events, issues=issues)

    def _validate_local(self, cards, *, tasks, evidence, novelty_points, paper_id,
                        reference_store, external_metadata=None) -> ValidationResult:
        from ..core.integrity_gates import validate_card_integrity

        fields = self.validate_fields(cards, tasks=tasks)
        integrity = validate_card_integrity(
            cards, evidence=evidence, tasks=tasks,
            novelty_points=novelty_points, paper_id=paper_id,
            reference_store=reference_store,
            external_metadata=external_metadata,
        )
        reasons_by_id: dict[str, list[str]] = {}
        for card_id, reason in fields.rejected:
            reasons_by_id.setdefault(card_id, []).append(reason)
        for card, reasons in integrity.rejected:
            reasons_by_id.setdefault(card.card_id, []).extend(reasons)
        accepted = tuple(card for card in integrity.accepted if card.card_id not in reasons_by_id)
        rejected = tuple((card.card_id, "; ".join(dict.fromkeys(reasons_by_id[card.card_id])))
                         for card in cards if card.card_id in reasons_by_id)
        issues = tuple(("evidence_integrity", f"Evidence Card {card.card_id}: {'; '.join(reasons)}",
                        "warning", card.task_id) for card, reasons in integrity.rejected)
        audit = {
            "validation_passed": not rejected, "checked_card_count": len(cards),
            "accepted_card_count": len(accepted), "rejected_card_count": len(rejected),
            "accepted_card_ids": [card.card_id for card in accepted],
            "rejected_card_ids": [card_id for card_id, _ in rejected],
            "rejected_cards": [{"card_id": card_id, "reasons": reasons_by_id[card_id]}
                               for card_id, _ in rejected],
            "provenance_rejected_card_ids": [card.card_id for card, _ in integrity.rejected],
        }
        return ValidationResult(accepted=accepted, rejected=rejected,
            issues=fields.issues + issues, integrity=audit)

    def validate_fields(
        self,
        cards: Sequence[EvidenceCard],
        *,
        tasks: Sequence[ResearchTask],
    ) -> ValidationResult:
        """Field-only precheck for isolated fixtures; not production validation."""
        task_by_key = {
            (task.novelty_point_id, task.task_id): task for task in tasks
        }
        accepted: list[EvidenceCard] = []
        rejected: list[tuple[str, str]] = []
        issues: list[tuple[str, str, str, str | None]] = []

        counts = Counter(card.card_id for card in cards)
        for card in cards:
            try:
                EvidenceCard.model_validate(card.model_dump(mode="json"))
                reason = (f"duplicate card_id: {card.card_id}" if counts[card.card_id] != 1
                          else self._rejection_reason(card, task_by_key))
            except ValidationError as exc:
                reason = f"invalid Card schema: {exc}"
            if reason is not None:
                rejected.append((card.card_id, reason))
                issues.append(
                    (
                        "invalid_evidence",
                        f"Evidence Card {card.card_id} 被拒绝：{reason}",
                        "warning",
                        card.task_id,
                    )
                )
                continue

            accepted.append(card)
        return ValidationResult(
            accepted=tuple(accepted),
            rejected=tuple(rejected),
            issues=tuple(issues),
        )

    def _rejection_reason(
        self,
        card: EvidenceCard,
        task_by_key: dict[tuple[str, str], ResearchTask],
    ) -> str | None:
        task = task_by_key.get((card.novelty_point_id, card.task_id))
        if task is None:
            return "task_id 不属于当前工作流"
        if not card.sources:
            return "缺少可追溯文献来源"
        if not any(source.doi or source.url for source in card.sources):
            return "来源缺少 DOI 或 URL"
        if self.config.require_direct_quote and not any(
            source.quote for source in card.sources
        ):
            return "缺少原文摘录"
        if self.config.require_source_location and not any(
            source.location for source in card.sources
        ):
            return "缺少原文位置"
        return None
