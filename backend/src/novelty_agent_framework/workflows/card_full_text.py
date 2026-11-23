"""Acquire database papers selected for grounded cards after the model has finished."""
from __future__ import annotations

from backend.env import ModelToolCall

from ..core.runtime_artifacts import current_runtime_artifacts
from ..core.tool_call_harness import ToolCallHarnessEvent
from ..schemas.failures import RecoveryAction


async def acquire_card_full_texts(registry, request, result):
    recovery = request.recovery
    if not result.evidence_cards or "database_search" not in registry.names:
        return (), []
    if recovery and recovery.action in {RecoveryAction.CHANGE_PROVIDER, RecoveryAction.RETRY_REQUEST}:
        return (), []  # These directives authorize discovery only.
    database = registry.get("database_search")
    # Grounded evidence carries the record ID. Discovery or the existing
    # reference catalog supplies its provider, also for previously saved reads.
    record_sources = {row.source_record_id: row.source_id
                      for bundle in result.research_bundles for row in bundle.source_records}
    # Material providers promise an original; legacy text providers deliver
    # extracted bodies. A historical .txt is still readable in either case.
    available = set()
    for bundle in result.research_bundles:
        for artifact in bundle.artifacts:
            source = record_sources.get(artifact.source_record_id)
            provider = database.tools_by_source.get(source)
            if getattr(provider, "material_provider", None) is not None:
                ready = artifact.role == "full_text" and artifact.provenance.get("material_representation") == "raw"
            else:
                ready = artifact.role in {"full_text", "extracted_text"}
            if ready and provider is not None:
                available.add(artifact.source_record_id)
    warnings = []
    catalog_loaded = False
    evidence = {item.evidence_id: item for item in result.evidence}
    by_source = {}
    for card in result.evidence_cards:
        for evidence_id in card.evidence_ids:
            origin = evidence[evidence_id].provenance
            if (origin.get("artifact_namespace") != "research_reference" or
                    origin.get("source_kind") != "structured_database"):
                continue
            record_id = origin.get("source_record_id")
            if not record_id or record_id in available:
                continue
            if record_id not in record_sources and not catalog_loaded:
                catalog_loaded = True
                try:
                    manifest = database.reference_store.load_manifest(request.subject_paper_id)
                except (OSError, ValueError) as exc:
                    warnings.append(f"Card full-text acquisition could not read the source catalog: {exc}")
                else:
                    record_sources.update({row.source_record_id: row.source_id for row in manifest.source_records})
            source = record_sources.get(record_id)
            if source not in database.tools_by_source:
                continue
            if recovery and (recovery.source_id and recovery.source_id != source or
                             recovery.action == RecoveryAction.FETCH_FULLTEXT and
                             record_id not in recovery.source_record_ids):
                continue
            records = by_source.setdefault(source, {})
            card_ids = records.setdefault(record_id, [])
            if card.card_id not in card_ids:
                card_ids.append(card.card_id)

    events = []
    for source, records in by_source.items():
        ids = list(records)
        # DatabaseSearchArguments accepts at most four records from one source.
        for start in range(0, len(ids), 4):
            batch = ids[start:start + 4]
            cards = {record: records[record] for record in batch}
            event = await _fetch_batch(registry, request, source, cards, len(events) + 1)
            events.append(event)
    return tuple(events), warnings


async def _fetch_batch(registry, request, source, cards, sequence):
    arguments = {"source_id": source, "full_text_source_record_ids": list(cards)}
    runtime = current_runtime_artifacts()
    handle = (runtime.start_tool_call("database_search", agent_arguments=None, resolved_arguments=arguments)
              if runtime else None)
    try:
        observation = await registry.execute("database_search", arguments, scope=request)
        if handle:
            items = observation.payload.get("database_search_result", {}).get("results", [])
            artifacts = {item["source_record_id"]: item.get("full_text_artifact_ids", []) for item in items}
            files = {key: artifacts.get(key, []) for key in cards}
            count = sum(bool(ids) for ids in files.values())
            if not observation.succeeded:
                status = "FAILED"
            elif count == len(cards):
                status = "ACQUIRED"
            else:
                status = "PARTIAL" if count else "UNAVAILABLE"
            runtime.finish_tool_call(
                handle, raw_result=observation.model_dump(mode="json"),
                normalized_result={"trigger": "card_full_text", "card_ids_by_record": cards,
                                   "full_text_artifact_ids": files},
                result_count=count, business_status=status,
                succeeded=observation.succeeded, error=observation.error,
            )
    except BaseException as exc:
        if handle:
            runtime.fail_tool_call(handle, exc)
        raise
    call_id = handle.tool_call_id if handle and handle.tool_call_id else f"card_full_text_{sequence}"
    call = ModelToolCall(id=call_id, name="database_search", arguments=arguments)
    return ToolCallHarnessEvent(kind="tool_result", tool_call=call,
                                observation=observation, detail="card_full_text")
