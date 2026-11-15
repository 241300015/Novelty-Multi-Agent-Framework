"""Project point-level Harness adoption without changing retained card facts."""
from collections.abc import Mapping


def card_available_for_report(card_id: str, state: Mapping) -> bool:
    if "gate_status" in state:
        return state["gate_status"] == "passed" and card_id in state.get("candidate_card_ids", [])
    if card_id in state.get("discarded_card_ids", []):
        return False
    # Pending/failed summaries still expose undiscarded partial card results.
    # An absent state belongs to the legacy contract, not an empty adoption.
    if state.get("stage") in {"draft", "complete"} and "adopted_card_ids" in state:
        return card_id in state["adopted_card_ids"]
    return True


def report_evidence_cards(cards, reviews):
    states = {r.novelty_point_id: review_report_state(r) for r in reviews}
    return [c for c in cards if card_available_for_report(
        c.card_id, states.get(c.novelty_point_id, {}))]


def report_review_context(review):
    """The model sees adopted sources; the persisted review keeps its audit ledger."""
    context = review.model_dump(mode="json")
    context["review_evidence"] = [e for e in context["review_evidence"]
        if card_available_for_report(e["origin_card_id"], review_report_state(review))]
    return context


def report_adoption_state(data, conclusion):
    """Older reports may retain the state only in the same-run review artifact."""
    if conclusion.get("report_disposition") is not None:
        return conclusion["report_disposition"]
    if conclusion.get("summary_harness_state"):
        return conclusion["summary_harness_state"]
    return next((r.get("report_disposition") or r.get("summary_harness_state", {})
                 for r in data.get("reviews", {}).get("reviews", [])
                 if r.get("novelty_point_id") == conclusion.get("novelty_point_id")), {})


def review_report_state(review):
    if review.report_disposition is not None:
        return review.report_disposition.model_dump(mode="json")
    return review.summary_harness_state
