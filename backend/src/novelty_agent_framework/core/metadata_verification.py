"""Validator-owned comparison of external metadata cards with bound Evidence.

Retrieval assembles candidates; this module alone decides identity consistency.
No local manifests or Evidence objects are rewritten after an external query.
"""

from dataclasses import asdict

from ..ports import MetadataQuery, ValidationResult
from ..schemas import ArtifactNamespace
from ..tools.database_search.providers.doi_common import normalize_doi
from ..tools.evidence_card_builder import normalize_quote_whitespace
from .integrity_gates import (
    MISSING_DOI_METADATA, MISSING_URL_METADATA, _evidence_namespace, _manifest_indexes,
)


def _text(value):
    return " ".join((value or "").split()).casefold()


def _local_field(records, name):
    """Use only mutually consistent observed local facts, including SourceRecords."""
    values = {}
    for record in records:
        value = getattr(record, name, None)
        if value is None or value == [] or value == "":
            continue
        key = tuple(map(_text, value)) if isinstance(value, list) else value
        values.setdefault(key, value)
    if len(values) > 1:
        raise ValueError(f"inconsistent local {name}")
    return next(iter(values.values()), None)


def compare_metadata(query, result):
    """Conservative deterministic identity policy, not content/novelty validation.

    Known DOI: matching DOI and title are required. Without a DOI: matching
    title and at least one comparable author/year field are required. Available
    authors/year/venue must agree; ambiguity/conflicts/missing facts stay unverified.
    """
    matched = []
    conflicts = []
    uncertain = []
    for card in result.cards:
        metadata = card.metadata
        if metadata.record_kind != "bibliographic":
            uncertain.append((card.candidate_id, "registration_without_bibliographic_identity"))
            continue
        if any(name in card.conflicts for name in ("doi", "title", "authors", "year", "venue")):
            uncertain.append((card.candidate_id, "conflicting_external_fields"))
            continue
        if not metadata.title or not query.title:
            uncertain.append((card.candidate_id, "missing_title_for_comparison"))
            continue
        if query.doi and (not metadata.doi or normalize_doi(metadata.doi) != normalize_doi(query.doi)):
            continue
        if _text(metadata.title) != _text(query.title):
            if query.doi:
                conflicts.append((card.candidate_id, "doi_points_to_different_title"))
            continue
        corroboration = 0
        differences = []
        if query.authors and metadata.authors:
            if tuple(map(_text, query.authors)) != tuple(map(_text, metadata.authors)):
                differences.append("authors_differ")
            else:
                corroboration += 1
        if query.year is not None and metadata.year is not None:
            if query.year != metadata.year:
                differences.append("publication_year_differs")
            else:
                corroboration += 1
        if query.venue and metadata.venue and _text(query.venue) != _text(metadata.venue):
            differences.append("venue_differs")
        if differences:
            uncertain.append((card.candidate_id, ",".join(differences)))
        elif query.doi or corroboration:
            matched.append(card)
        else:
            uncertain.append((card.candidate_id, "title_only_without_corroboration"))
    if len(matched) == 1 and not conflicts and not uncertain:
        return "verified", matched[0], "metadata_matches_bound_work"
    if len(matched) > 1:
        return "unverified", None, "multiple_matching_candidates"
    if conflicts:
        return "conflict", None, "doi_points_to_different_title"
    if uncertain:
        return "unverified", None, uncertain[0][1]
    if any(provider.status == "error" for provider in result.providers):
        return "unverified", None, "external_lookup_incomplete"
    return "unverified", None, "no_matching_metadata_in_queried_sources"


def validate_with_external_metadata(validator, cards, *, tasks, evidence, novelty_points,
                                    paper_id, reference_store):
    kwargs = dict(tasks=tasks, evidence=evidence, novelty_points=novelty_points,
                  paper_id=paper_id, reference_store=reference_store)
    initial = validator._validate_local(cards, **kwargs)
    local_reasons = {item["card_id"]: item["reasons"] for item in initial.integrity["rejected_cards"]}
    indexes = _manifest_indexes(paper_id,
        {_evidence_namespace(item) for item in evidence} | {ArtifactNamespace.RESEARCH_REFERENCE},
        reference_store)
    by_id = {item.evidence_id: item for item in evidence}
    cache = {}
    observations = {}
    overlays = {}
    not_verified = {}
    for card in cards:
        reasons = local_reasons.get(card.card_id, [])
        if any(reason not in {MISSING_DOI_METADATA, MISSING_URL_METADATA} for reason in reasons):
            observations[card.card_id] = {"status": "not_run", "reason": "local_integrity_failed", "sources": []}
            continue
        checks = []
        resolved = [by_id[item] for item in card.evidence_ids if item in by_id]
        for source_index, source in enumerate(card.sources):
            bindings = {}
            for item in resolved:
                if normalize_quote_whitespace(source.quote or "") != normalize_quote_whitespace(item.quote):
                    continue
                namespace = _evidence_namespace(item)
                index = indexes[namespace]
                work = index.works.get(item.work_id)
                records = [record for record in index.records.values() if record.work_id == item.work_id]
                if work and any(_text(source.title) == _text(record.title) for record in [work, *records]):
                    binding = bindings.setdefault((namespace, item.work_id), (work, records, []))
                    binding[2].append(item.evidence_id)
            check = {"source_index": source_index,
                     "evidence_ids": [eid for _, _, ids in bindings.values() for eid in ids],
                     "status": "unverified", "reason": "ambiguous_source_work_binding"}
            checks.append(check)
            if len(bindings) != 1:
                continue
            key, (work, records, _) = next(iter(bindings.items()))
            check.update(work_id=work.work_id, artifact_namespace=key[0].value)
            try:
                local_dois = {normalize_doi(i.value) for record in [work, *records]
                              for i in record.identifiers if i.namespace == "doi"}
                if not source.doi and len(local_dois) > 1:
                    check["reason"] = "multiple_local_dois"
                    continue
                query = MetadataQuery(
                    doi=normalize_doi(source.doi) if source.doi else next(iter(local_dois), None),
                    title=source.title, authors=tuple(_local_field([work, *records], "authors") or ()),
                    year=_local_field([work, *records], "publication_year"),
                    venue=work.venue,
                )
            except ValueError:
                check["reason"] = "invalid_local_query_metadata"
                continue
            if query not in cache:
                if len(cache) >= validator.metadata_query_limit:
                    check["reason"] = "metadata_query_budget_exhausted"
                    continue
                cache[query] = validator.metadata_retrieval.retrieve(query)
            result = cache[query]
            status, matched, reason = compare_metadata(query, result)
            check.update(status=status, reason=reason, retrieval=asdict(result))
            if matched:
                check["matched_candidate_id"] = matched.candidate_id
                overlays[key] = tuple({**{f"prior:{i}": r for i, r in enumerate(overlays.get(key, ()))},
                                       **matched.records}.values())
        status = ("conflict" if any(item["status"] == "conflict" for item in checks)
                  else "verified" if checks and all(item["status"] == "verified" for item in checks)
                  else "unverified")
        observations[card.card_id] = {"status": status, "sources": checks}
        if status != "verified":
            not_verified[card.card_id] = f"metadata_authenticity_{status}: " + "; ".join(
                item["reason"] for item in checks if item["status"] != "verified")

    # Recheck the original evidence/files after I/O; no mutation or shadow manifest.
    final = validator._validate_local(cards, external_metadata=overlays, **kwargs)
    rejected = list(final.rejected)
    rejected_ids = {item[0] for item in rejected}
    for card_id, reason in not_verified.items():
        if card_id in rejected_ids:
            rejected = [(cid, f"{message}; {reason}" if cid == card_id else message) for cid, message in rejected]
        else:
            rejected.append((card_id, reason))
            rejected_ids.add(card_id)
    accepted = tuple(card for card in final.accepted if card.card_id not in rejected_ids)
    audit = dict(final.integrity)
    audit.update(metadata_authenticity=observations, metadata_query_count=len(cache),
        validation_passed=not rejected, accepted_card_count=len(accepted), rejected_card_count=len(rejected),
        accepted_card_ids=[card.card_id for card in accepted], rejected_card_ids=[cid for cid, _ in rejected],
        rejected_cards=[{"card_id": cid, "reasons": [reason]} for cid, reason in rejected])
    issues = list(final.issues)
    task_ids = {card.card_id: card.task_id for card in cards}
    for card_id, reason in not_verified.items():
        issues.append(("metadata_authenticity_" + observations[card_id]["status"],
                       reason, "warning", task_ids[card_id]))
    return ValidationResult(accepted=accepted, rejected=tuple(rejected), issues=tuple(issues), integrity=audit)
