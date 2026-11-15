"""One renderer input adapter: frozen report sources, or the historical contract."""
from copy import deepcopy


def report_view(data):
    sources = data["report"].get("sources")
    if sources is None:
        return data  # Legacy artifacts remain read-only; version checks still apply.
    if sources.get("schema_version") != 1:
        raise ValueError("unsupported report sources version")
    view = deepcopy(data)
    view["reviews"] = {
        "phase": "gate_a_complete",
        "card_reviews": sources["card_reviews"],
        "reviews": [{**c, "status": c.get("review_status")}
                    for c in view["report"].get("conclusions", [])],
        "integrity": data.get("gate_a", {}),
    }
    return view


def report_card_rows(data):
    sources = data["report"].get("sources")
    if sources is not None:
        return sources["card_reviews"]
    # Old point-shaped and support-shaped card records share this adapter only.
    return [*(data.get("reviews", {}).get("card_reviews") or []),
            *(data["report"].get("partial_card_reviews") or [])]
