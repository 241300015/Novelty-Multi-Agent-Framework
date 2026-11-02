"""Shared report wording for unavailable evidence and incomplete reviews."""

NO_EFFECTIVE_EVIDENCE = "本次查新未得到有效证据"

INCOMPLETE_REVIEW_LABELS = {
    "semantic_evidence": "比较证据不足，尚不能作出新颖性判断",
    "material_unavailable": "评审所需材料不足，尚不能作出新颖性判断",
    "technical_error": "文献比较评审发生技术错误，尚不能作出新颖性判断",
    "budget_exhausted": "文献比较评审预算已耗尽，尚不能作出新颖性判断",
}

LEGACY_REVIEW_SUMMARIES = {
    "Reviewer 核验未完成，尚不能作出新颖性裁定。",
    "该查新点的关键比较证据不足，尚不能作出新颖性裁定。",
}


def incomplete_review_label(reason: str | None) -> str:
    return INCOMPLETE_REVIEW_LABELS.get(reason, "评审未形成结论，具体原因未记录")


def incomplete_review_summary(reason: str | None, *, has_cards: bool = True) -> str:
    return (incomplete_review_label(reason) if has_cards else NO_EFFECTIVE_EVIDENCE) + "。"
