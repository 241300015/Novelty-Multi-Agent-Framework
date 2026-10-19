"""Evidence Card 的确定性合法性与溯源门控。"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

from pydantic import ValidationError
from typing import Sequence

from ..schemas import Evidence, EvidenceCard, NoveltyPoint, ResearchTask
from ..ports import ValidationResult
from ..persistence import ReferenceStore


@dataclass(frozen=True)
class EvidenceValidationConfig:
    """引文和位置字段要求；不根据模型评分筛选证据。"""

    require_direct_quote: bool = True
    require_source_location: bool = True


class DefaultEvidenceValidator:
    """拒绝字段不合法、不可追溯或任务归属错误的证据。"""

    def __init__(self, config: EvidenceValidationConfig | None = None) -> None:
        self.config = config or EvidenceValidationConfig()

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
        from ..core.integrity_gates import validate_card_integrity

        fields = self.validate_fields(cards, tasks=tasks)
        integrity = validate_card_integrity(
            cards, evidence=evidence, tasks=tasks,
            novelty_points=novelty_points, paper_id=paper_id,
            reference_store=reference_store,
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
