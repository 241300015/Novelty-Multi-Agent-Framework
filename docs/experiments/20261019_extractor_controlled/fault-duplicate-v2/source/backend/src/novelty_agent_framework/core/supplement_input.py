"""Build compact planning inputs and verify indexes against persisted records."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from ..schemas.supplement_planning import PersistedPlanningMaterial, SupplementPlanningInput
from .review_validation import feature_catalog


def build_supplement_input(*, paper, brief, decisions, evidence, gaps, languages,
                           attempt, history, run_id=None, material_index=()):
    point_ids = {d.point_id for d in decisions}
    return SupplementPlanningInput.model_validate({
        "run_id": run_id,
        "paper": paper.model_dump(include={"paper_id", "title", "abstract"}),
        "brief": {**brief.model_dump(exclude={"research_tasks", "novelty_points"}),
                  "novelty_points": [{**p.model_dump(), "feature_catalog": feature_catalog(p)}
                                     for p in brief.novelty_points if p.point_id in point_ids]},
        "recovery_decisions": list(decisions),
        "existing_evidence": [c.model_dump(include={"card_id", "novelty_point_id", "document_title",
                              "main_contribution", "overlaps", "differences"})
                              for c in evidence if c.novelty_point_id in point_ids],
        "insufficient_final_evidence_points": [
            {**g.model_dump(), "additional_cards_needed": g.required_card_count - g.valid_card_count}
            for g in gaps if g.novelty_point_id in point_ids],
        "research_languages": list(languages), "attempt": attempt,
        "research_history": [h for h in history if h.point_id in point_ids],
        "material_index": list(material_index),
    })


def persisted_material_index(*, output_root, workspace, paper, points, cards,
                             tasks, results, reviews, decisions, run_id, round_number):
    """Index only exact full-record matches; stale/unreadable/missing files are omitted.

    Legacy files are paper-scoped, so existence or matching object IDs alone
    cannot establish that their content matches this planning input.
    """
    root, workspace = Path(output_root).resolve(), Path(workspace)
    allowed = {d.point_id for d in decisions}
    index, cache = [], {}

    def add(relative, pointer, expected, kind, **bindings):
        path = (workspace / relative).resolve()
        if not path.is_relative_to(root):
            return
        if path not in cache:
            try:
                raw = path.read_bytes()
                cache[path] = (json.loads(raw), hashlib.sha256(raw).hexdigest())
            except (OSError, ValueError):
                cache[path] = None
        if cache[path] is None:
            return
        data, digest = cache[path]
        try:
            selected = data
            tokens = pointer.split("/")[1:]
            for position, token in enumerate(tokens):
                token = token.replace("~1", "/").replace("~0", "~")
                if isinstance(selected, list) and position == len(tokens) - 1:
                    # Aggregate ordering can change without changing the record.
                    token = str(selected.index(expected))
                    tokens[position] = token
                selected = selected[int(token)] if isinstance(selected, list) else selected[token]
        except (KeyError, IndexError, TypeError, ValueError):
            return
        if selected != expected:
            return
        # All aggregate files identify the paper; task result files do not.
        if isinstance(data, dict) and data.get("paper_id", paper.paper_id) != paper.paper_id:
            return
        if kind == "recovery_decision" and data.get("run_id") != run_id:
            return
        index.append(PersistedPlanningMaterial(material_id=f"M{len(index) + 1}",
            kind=kind, path=path.relative_to(root).as_posix(),
            json_pointer="/" + "/".join(tokens) if tokens else "", sha256=digest, **bindings))

    add("paper-input/others/paper.json", "", paper.model_dump(mode="json"), "paper")
    for i, p in enumerate(points):
        if p.point_id in allowed:
            add("novelty-points.json", f"/novelty_points/{i}", p.model_dump(mode="json"),
                "novelty_point", point_id=p.point_id)
    for i, c in enumerate(cards):
        if c.novelty_point_id in allowed:
            add("evidence-cards.json", f"/accepted_evidence_cards/{i}", c.model_dump(mode="json"),
                "evidence_card", point_id=c.novelty_point_id, card_id=c.card_id)
    by_key = {(r.novelty_point_id, r.task_id): r for r in results}
    for t in tasks:
        result = by_key.get((t.novelty_point_id, t.task_id))
        if result is not None and t.novelty_point_id in allowed:
            executions = [*result.search_executions,
                          *(e for b in result.research_bundles for e in b.search_executions)]
            if any(e.run_id is not None and e.run_id != run_id for e in executions):
                continue
            # IDs originate in runtime tasks; resolved containment is checked by add().
            add(f"research-runs/{t.novelty_point_id}/{t.task_id}/attempt-{t.attempt}.json", "",
                result.model_dump(mode="json"), "task_result", point_id=t.novelty_point_id,
                task_id=t.task_id, attempt=t.attempt)
    for i, r in enumerate(reviews):
        if r.novelty_point_id in allowed:
            add("novelty-reviews.json", f"/reviews/{i}", r.model_dump(mode="json"),
                "review", point_id=r.novelty_point_id)
    # Gate audit also includes STOP decisions, so locate by its persisted order.
    if run_id:
        relative = f"recovery/{run_id}/round-{round_number}-gate.json"
        for i, d in enumerate(decisions):
            add(relative, f"/decisions/{i}", d.model_dump(mode="json"),
                "recovery_decision", point_id=d.point_id, trigger_code=d.trigger_codes[0])
    return index
