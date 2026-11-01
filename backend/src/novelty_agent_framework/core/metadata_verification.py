"""Validator-owned identity comparison with concrete, catalogued failure causes."""

from dataclasses import asdict, dataclass, replace
import json

from ..ports import MetadataQuery, ValidationResult
from ..schemas import ArtifactNamespace
from ..schemas.failures import (
    CodedFailure, FAILURE_DESCRIPTIONS, FailureCode as C, FailureScope, make_failure,
    RecoveryAction, RetryAdvice,
)
from ..tools.database_search.providers.doi_common import normalize_doi
from ..tools.evidence_card_builder import normalize_quote_whitespace
from .correction_trace import record_detection
from .author_names import author_names_key
from .runtime_artifacts import current_runtime_artifacts
from .integrity_gates import (
    MISSING_DOI_METADATA, MISSING_URL_METADATA, _evidence_namespace, _manifest_indexes,
)


@dataclass(frozen=True)
class MetadataFinding:
    code: C
    reason: str
    candidate_id: str | None = None
    fields: tuple[str, ...] = ()


def _text(value):
    return " ".join((value or "").split()).casefold()


def _value(value):
    return _text(value) if isinstance(value, str) else value


def _local_field(records, name):
    values = {}
    for record in records:
        value = getattr(record, name, None)
        if value is None or value == [] or value == "":
            continue
        key = author_names_key(value) if name == "authors" and isinstance(value, list) else (tuple(map(_text, value)) if isinstance(value, list) else value)
        values.setdefault(key, value)
    if len(values) > 1:
        raise CodedFailure(C.VALIDATOR_METADATA_LOCAL_CONFLICT, f"本地字段 {name} 存在不同值")
    return next(iter(values.values()), None)


def compare_metadata(query, result):
    """Return passed/failed plus all observed comparison failures, never a catch-all label.

    A provider failure is recorded separately, even when another source proves
    identity. No-match only describes the returned candidate set, not all literature.
    """
    matched, findings = [], []
    for card in result.cards:
        metadata = card.metadata
        def finding(code, reason, fields=()):
            findings.append(MetadataFinding(code, reason, card.candidate_id, tuple(fields)))
        if card.conflicts:
            fields = tuple(card.conflicts)
            finding(C.VALIDATOR_METADATA_SOURCE_CONFLICT, "外部来源字段冲突：" + ", ".join(fields), fields)
        if metadata.record_kind != "bibliographic":
            finding(C.VALIDATOR_METADATA_REGISTRATION_ONLY, "Handle 存在，但缺少可比对的题录")
            continue
        if query.doi and metadata.doi and normalize_doi(metadata.doi) != normalize_doi(query.doi):
            finding(C.VALIDATOR_METADATA_IDENTITY_MISMATCH, "返回 DOI 与待核对 DOI 不一致", ("doi",))
            continue
        has_common_doi = bool(query.doi and metadata.doi)
        differences = []
        if metadata.title and query.title and _text(metadata.title) != _text(query.title):
            differences.append("title")
        if query.authors and metadata.authors and author_names_key(query.authors) != author_names_key(metadata.authors):
            differences.append("authors")
        if query.year is not None and metadata.year is not None and query.year != metadata.year:
            differences.append("year")
        for field in (("venue",) if has_common_doi else ("venue", "publisher", "volume", "issue", "pages", "page_count")):
            local, external = getattr(query, field), getattr(metadata, field)
            if local is not None and external is not None and _value(local) != _value(external):
                differences.append(field)
        if differences:
            finding(C.VALIDATOR_METADATA_IDENTITY_MISMATCH,
                    "外部题录与本地文献字段不一致：" + ", ".join(differences), differences)
            continue
        required = ("title",) if has_common_doi else ("title", "authors", "year")
        missing = tuple(field for field in required if not getattr(query, field) or not getattr(metadata, field))
        if missing:
            finding(C.VALIDATOR_METADATA_FIELDS_MISSING, "缺少比对所需字段：" + ", ".join(missing), missing)
            continue
        matched.append(card)
    if matched:
        return "passed", matched[0], tuple(findings)
    if not findings:
        if all(provider.status == "unsupported" for provider in result.providers):
            findings.append(MetadataFinding(C.VALIDATOR_METADATA_UNSUPPORTED, "所有配置来源均不支持该查询"))
        elif result.cards or any(provider.status in {"found", "not_found"} for provider in result.providers):
            findings.append(MetadataFinding(C.VALIDATOR_METADATA_NO_MATCH, "已返回的候选中无匹配；未穷尽全部数据库"))
    return "failed", None, tuple(findings)



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
    task_by_key = {(item.novelty_point_id, item.task_id): item for item in tasks}
    cache, observations, overlays, metadata_rejections = {}, {}, {}, {}
    events = []
    runtime = current_runtime_artifacts()

    def emit(card, code, reason, *, check=None, provider=None, candidate_id=None, fields=(), occurrence=""):
        task = task_by_key.get((card.novelty_point_id, card.task_id))
        refs = list(check["evidence_ids"] if check is not None else card.evidence_ids)
        if check and check.get("work_id"):
            refs.append(f"{check['artifact_namespace']}:work:{check['work_id']}")
        if candidate_id:
            refs.append(candidate_id)
        message = f"{FAILURE_DESCRIPTIONS[code]}；{reason}"
        if check is not None:
            message = f"来源项 {check['source_index']}：{message}"
        event = make_failure(code, scope=FailureScope(node="validate_evidence", paper_id=paper_id,
            run_id=runtime.run_id if runtime else None,
            point_id=card.novelty_point_id, task_id=card.task_id, card_id=card.card_id,
            attempt=getattr(task, "attempt", None), provider=provider), message=message,
            evidence_refs=refs, occurrence_id=json.dumps([
                check.get("source_index") if check else None, candidate_id, fields, reason, occurrence,
            ], ensure_ascii=False))
        events.append(event)
        if check is not None:
            check["failure_event_ids"].append(event.event_id)
        return event

    for card in cards:
        reasons = local_reasons.get(card.card_id, [])
        if any(reason not in {MISSING_DOI_METADATA, MISSING_URL_METADATA} for reason in reasons):
            event = emit(card, C.VALIDATOR_METADATA_LOCAL_INTEGRITY, "; ".join(reasons))
            observations[card.card_id] = {"status": "skipped", "query_status": "not_started",
                "sources": [], "failure_event_ids": [event.event_id]}
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
                    bindings.setdefault((namespace, item.work_id), (work, records, []))[2].append(item.evidence_id)
            check = {"source_index": source_index,
                     "evidence_ids": [eid for _, _, ids in bindings.values() for eid in ids],
                     "status": "failed", "query_status": "not_started", "failure_event_ids": []}
            checks.append(check)
            if len(bindings) != 1:
                emit(card, C.VALIDATOR_METADATA_BINDING_AMBIGUOUS,
                     f"可绑定的 Work 数量为 {len(bindings)}", check=check)
                continue
            key, (work, records, _) = next(iter(bindings.items()))
            check.update(work_id=work.work_id, artifact_namespace=key[0].value)
            try:
                local_dois = {normalize_doi(i.value) for record in [work, *records]
                              for i in record.identifiers if i.namespace == "doi"}
                if not source.doi and len(local_dois) > 1:
                    raise CodedFailure(C.VALIDATOR_METADATA_LOCAL_CONFLICT, "本地记录含多个 DOI，无法确定查询标识符")
                query = MetadataQuery(
                    doi=normalize_doi(source.doi) if source.doi else next(iter(local_dois), None),
                    title=source.title, authors=tuple(_local_field([work, *records], "authors") or ()),
                    year=_local_field([work, *records], "publication_year"), venue=_local_field([work, *records], "venue"),
                    publisher=_local_field([work, *records], "publisher"),
                    volume=_local_field([work, *records], "volume"), issue=_local_field([work, *records], "issue"),
                    pages=_local_field([work, *records], "pages"),
                    page_count=_local_field([work, *records], "page_count"),
                )
            except CodedFailure as exc:
                emit(card, exc.code, str(exc), check=check)
                continue
            except ValueError:
                emit(card, C.VALIDATOR_METADATA_LOCAL_INVALID, "DOI 或题录查询字段不合法，未发出请求", check=check)
                continue
            check["query"] = asdict(query)
            if query not in cache:
                if len(cache) >= validator.metadata_query_limit:
                    emit(card, C.VALIDATOR_METADATA_QUERY_BUDGET,
                         f"已使用 {len(cache)}/{validator.metadata_query_limit} 个不同查询额度", check=check)
                    continue
                cache[query] = validator.metadata_retrieval.retrieve(query)
            result = cache[query]
            check.update(query_status=result.status, retrieval=asdict(result))
            for provider in result.providers:
                if provider.status == "error":
                    emit(card, provider.failure_code or C.PROVIDER_UNKNOWN,
                         f"{provider.source_id}: HTTP={provider.http_status}, error_type={provider.error_type}",
                         check=check, provider=provider.source_id)
            for provider in result.providers:
                if provider.status == "not_found":
                    emit(card, C.VALIDATOR_METADATA_NO_MATCH,
                         f"{provider.source_id}: 当前元数据核验未找到该文献", check=check,
                         provider=provider.source_id)
            status, matched, findings = compare_metadata(query, result)
            # A successful source can return only unrelated candidates while
            # another source fails. Keep that scoped miss alongside the failure.
            for provider in result.providers:
                if provider.status != "found":
                    continue
                candidates = [candidate for candidate in result.cards if any(
                    ref.startswith(provider.source_id + ":") for ref in candidate.records)]
                comparisons = [compare_metadata(query, replace(result, cards=(candidate,)))
                               for candidate in candidates]
                if comparisons and all(
                    candidate_status == "failed" and any(
                        finding.code == C.VALIDATOR_METADATA_IDENTITY_MISMATCH for finding in candidate_findings)
                    for candidate_status, _, candidate_findings in comparisons
                ):
                    emit(card, C.VALIDATOR_METADATA_NO_MATCH,
                         f"{provider.source_id}: 当前元数据核验未找到该文献", check=check,
                         provider=provider.source_id)
            check.update(status=status, findings=[asdict(item) for item in findings])
            for finding in findings:
                emit(card, finding.code, finding.reason, check=check,
                     candidate_id=finding.candidate_id, fields=finding.fields)
            if matched:
                check["matched_candidate_id"] = matched.candidate_id
                check["doi_comparison"] = "matched" if query.doi and matched.metadata.doi else "not_available"
                overlays[key] = (*overlays.get(key, ()), *matched.records.values())
        status = "passed" if checks and all(item["status"] == "passed" for item in checks) else "failed"
        observations[card.card_id] = {"status": status, "sources": checks,
            "failure_event_ids": [eid for item in checks for eid in item["failure_event_ids"]]}
        if status == "failed":
            ids = {eid for item in checks if item["status"] == "failed" for eid in item["failure_event_ids"]}
            metadata_rejections[card.card_id] = "; ".join(
                f"{event.code.value}: {event.message}" for event in events if event.event_id in ids)

    # External identity cannot excuse a file/quote/location failure after I/O.
    final = validator._validate_local(cards, external_metadata=overlays, **kwargs)
    for item in final.integrity["rejected_cards"]:
        if observations[item["card_id"]]["status"] == "skipped":
            continue
        reasons = [r for r in item["reasons"] if r not in {MISSING_DOI_METADATA, MISSING_URL_METADATA}]
        if reasons:
            card = next(card for card in cards if card.card_id == item["card_id"])
            emit(card, C.VALIDATOR_METADATA_LOCAL_INTEGRITY, "; ".join(reasons), occurrence="after_lookup")
    rejected = list(final.rejected)
    rejected_ids = {item[0] for item in rejected}
    for card_id, reason in metadata_rejections.items():
        if card_id in rejected_ids:
            rejected = [(cid, f"{message}; {reason}" if cid == card_id else message) for cid, message in rejected]
        else:
            rejected.append((card_id, reason))
            rejected_ids.add(card_id)
    accepted = tuple(card for card in final.accepted if card.card_id not in rejected_ids)
    for card_id, observation in observations.items():
        observation["final_result"] = "rejected" if card_id in rejected_ids else "accepted"
    accepted_ids = {card.card_id for card in accepted}
    # Failed sources or candidates remain auditable, but cannot say they
    # blocked a Card when a different candidate satisfied the OR policy.
    events = [event.model_copy(update={"execution_status": "partial",
        "conclusion_effect": "limits_coverage", "recovery": RecoveryAction.NONE,
        "retry": RetryAdvice()}) if event.scope.card_id in accepted_ids else event for event in events]
    events = list({event.event_id: event for event in events}.values())
    for event in events:
        record_detection(event, owner_node="validate_evidence", owner_function="DefaultEvidenceValidator.validate",
            targets={"card_ids": [event.scope.card_id],
                     "evidence_ids": [ref for ref in event.evidence_refs if ref in by_id],
                     "source_ids": [event.scope.provider] if event.scope.provider else []})
    audit = dict(final.integrity)
    audit.update(metadata_mode="external", metadata_authenticity=observations, metadata_query_count=len(cache),
        failure_events=[event.model_dump(mode="json") for event in events],
        validation_passed=not rejected, accepted_card_count=len(accepted), rejected_card_count=len(rejected),
        accepted_card_ids=[card.card_id for card in accepted], rejected_card_ids=[cid for cid, _ in rejected],
        rejected_cards=[{"card_id": cid, "reasons": [reason]} for cid, reason in rejected])
    issues = (*final.issues, *((event.code.value, event.message, "warning", event.scope.task_id) for event in events))
    return ValidationResult(accepted=accepted, rejected=tuple(rejected), issues=issues,
                            integrity=audit, failure_events=tuple(events))
