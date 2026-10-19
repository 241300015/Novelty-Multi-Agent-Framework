"""Conservative target-paper identity checks at the retrieval boundary."""

from __future__ import annotations

import json
import re
import unicodedata
from copy import deepcopy
from difflib import SequenceMatcher
from urllib.parse import unquote, urlparse

from ..schemas.domain import PaperInput
from ..schemas.research import TargetPaperIdentity
from ..schemas.references import ExternalIdentifier
from ..schemas.research import ResearcherToolObservation
from ..persistence import reference_store_for_artifact_namespace


def identity_from_paper(paper: PaperInput) -> TargetPaperIdentity:
    metadata = {key.casefold(): value for key, value in paper.metadata.items()}
    alternate = [metadata[key] for key in ("english_title", "title_en", "alternate_title")
                 if metadata.get(key)]
    authors: list[str] = []
    for key in ("authors", "author", "first_author"):
        value = metadata.get(key)
        if not value:
            continue
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            parsed = None
        if isinstance(parsed, list):
            authors.extend(str(item).strip() for item in parsed if str(item).strip())
        else:
            authors.extend(part.strip() for part in re.split(r"[;,；，]", value) if part.strip())
    identifiers = []
    for key, namespace in (("doi", "doi"), ("arxiv_id", "arxiv"),
                           ("arxiv", "arxiv"), ("pmid", "pmid"),
                           ("provider_document_id", "provider_document_id")):
        if metadata.get(key):
            identifiers.append(ExternalIdentifier(namespace=namespace, value=metadata[key]))
    if metadata.get("provider_document_id") and (metadata.get("provider") or metadata.get("source_id")):
        identifiers.append(ExternalIdentifier(
            namespace=metadata.get("provider") or metadata["source_id"],
            value=metadata["provider_document_id"],
        ))
    for key in ("canonical_url", "url", "paper_url"):
        if metadata.get(key):
            for namespace, value in _ids_from_url(metadata[key]):
                identifiers.append(ExternalIdentifier(namespace=namespace, value=value))
    return TargetPaperIdentity(
        title=paper.title, alternate_titles=list(dict.fromkeys(alternate)),
        authors=list(dict.fromkeys(authors)), identifiers=identifiers,
    )


def normalize_title(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).casefold()
    return " ".join("".join(char if char.isalnum() else " " for char in value).split())


def match_target(identity: TargetPaperIdentity, candidate: dict) -> str | None:
    target_ids = {_canonical_id(item.namespace, item.value)
                  for item in identity.identifiers}
    target_ids.discard(None)
    candidate_ids = set()
    for row in candidate.get("identifiers", []):
        if isinstance(row, dict):
            candidate_ids.add(_canonical_id(str(row.get("namespace", "")), str(row.get("value", ""))))
    source = str(candidate.get("source_id", "")).casefold()
    external = candidate.get("external_id")
    if external:
        candidate_ids.add(_canonical_id(source, str(external)))
    for key in ("landing_url", "full_text_url"):
        if candidate.get(key):
            candidate_ids.update(_ids_from_url(str(candidate[key])))
    candidate_ids.discard(None)
    for namespace, value in target_ids & candidate_ids:
        return "arxiv_id" if namespace == "arxiv" else namespace

    title = normalize_title(str(candidate.get("title") or ""))
    targets = [normalize_title(value) for value in [identity.title, *identity.alternate_titles]]
    if not title or not any(targets):
        return None
    if title in targets:
        return "normalized_title"
    authors = [normalize_title(value) for value in candidate.get("authors", []) if isinstance(value, str)]
    target_authors = [normalize_title(value) for value in identity.authors]
    if authors and target_authors and max(SequenceMatcher(None, title, target).ratio()
                                          for target in targets if target) >= 0.94:
        first_matches = authors[0] == target_authors[0]
        overlap = len(set(authors) & set(target_authors)) / max(len(set(authors)), len(set(target_authors)))
        if first_matches or overlap >= 0.67:
            return "similar_title_and_authors"
    return None


def filter_target_observation(
    observation: ResearcherToolObservation, identity: TargetPaperIdentity,
) -> tuple[ResearcherToolObservation, set[str]]:
    """Remove the target from model-visible candidates and return blocked artifacts."""
    if observation.tool_name not in {"database_search", "reference_search"}:
        return observation, set()
    payload = deepcopy(observation.payload)
    exclusions: list[dict[str, str | None]] = []
    blocked_artifacts: set[str] = set()
    blocked_works: set[str] = set()
    blocked_records: set[str] = set()

    if observation.tool_name == "database_search":
        bundle = payload.get("research_bundle") or {}
        works = {row.get("work_id"): row for row in bundle.get("works", []) if isinstance(row, dict)}
        for record in bundle.get("source_records", []):
            if not isinstance(record, dict):
                continue
            work = works.get(record.get("work_id"), {})
            matched = match_target(identity, record) or match_target(identity, work)
            if not matched:
                continue
            work_id, record_id = record.get("work_id"), record.get("source_record_id")
            if work_id:
                blocked_works.add(work_id)
            if record_id:
                blocked_records.add(record_id)
            exclusions.append({"namespace": "research_reference", "source_record_id": record_id,
                               "work_id": work_id, "title": record.get("title"),
                               "status": "excluded", "excluded_reason": "target_paper",
                               "identity_match": matched})
        for artifact in bundle.get("artifacts", []):
            if artifact.get("work_id") in blocked_works or artifact.get("source_record_id") in blocked_records:
                blocked_artifacts.add(artifact["artifact_id"])
        if blocked_works or blocked_records:
            bundle["works"] = [row for row in bundle.get("works", []) if row.get("work_id") not in blocked_works]
            bundle["source_records"] = [row for row in bundle.get("source_records", [])
                                        if row.get("source_record_id") not in blocked_records
                                        and row.get("work_id") not in blocked_works]
            bundle["artifacts"] = [row for row in bundle.get("artifacts", [])
                                   if row.get("artifact_id") not in blocked_artifacts]
            bundle["evidence"] = [row for row in bundle.get("evidence", [])
                                  if row.get("work_id") not in blocked_works]
            result = payload.get("database_search_result", {})
            result["results"] = [row for row in result.get("results", [])
                                 if row.get("work_id") not in blocked_works
                                 and row.get("source_record_id") not in blocked_records]
            payload["source_records"] = bundle["source_records"]
            payload["artifacts"] = bundle["artifacts"]
            observation = observation.model_copy(update={
                "summary": f"数据库检索召回 {len(result['results'])} 个候选作品；排除原论文 {len(exclusions)} 条",
            })
    else:
        result = payload.get("reference_search_result", {})
        kept = []
        for row in result.get("results", []):
            matched = match_target(identity, row)
            if not matched:
                kept.append(row)
                continue
            blocked_works.add(row.get("work_id"))
            blocked_artifacts.update(handle["artifact_id"] for handle in row.get("artifact_handles", []))
            exclusions.append({"namespace": "subject_reference", "source_record_id": None,
                               "work_id": row.get("work_id"), "title": row.get("title"),
                               "status": "excluded", "excluded_reason": "target_paper",
                               "identity_match": matched})
        result["results"] = kept
        if exclusions:
            observation = observation.model_copy(update={
                "summary": f"从原论文参考文献中召回 {len(kept)} 条；排除原论文 {len(exclusions)} 条",
            })
    if not exclusions:
        return observation, set()
    payload["target_exclusions"] = exclusions
    return observation.model_copy(update={"payload": payload}), blocked_artifacts


def artifact_is_target(reader_tool: object, paper_id: str, artifact_id: str,
                       identity: TargetPaperIdentity) -> bool:
    """Recheck the persisted manifest so a guessed or cached handle cannot bypass the gate."""
    raw_reader = getattr(reader_tool, "reader", None)
    if raw_reader is None or not hasattr(raw_reader, "locate"):
        return False
    namespace = raw_reader.locate(paper_id, artifact_id)
    if namespace is None:
        return False
    store = reference_store_for_artifact_namespace(
        namespace, output_root=raw_reader.reference_store.output_root,
    )
    manifest = store.load_manifest(paper_id)
    artifact = next((item for item in manifest.artifacts if item.artifact_id == artifact_id), None)
    if artifact is None:
        return False
    work = next((item for item in manifest.works if item.work_id == artifact.work_id), None)
    record = next((item for item in manifest.source_records
                   if item.source_record_id == artifact.source_record_id), None)
    return bool((work and match_target(identity, work.model_dump(mode="json"))) or
                (record and match_target(identity, record.model_dump(mode="json"))))


def source_record_is_target(store: object, paper_id: str, source_record_id: str,
                            identity: TargetPaperIdentity) -> bool:
    manifest = store.load_manifest(paper_id)
    record = next((item for item in manifest.source_records
                   if item.source_record_id == source_record_id), None)
    if record is None:
        return False
    work = next((item for item in manifest.works if item.work_id == record.work_id), None)
    return bool(match_target(identity, record.model_dump(mode="json")) or
                (work and match_target(identity, work.model_dump(mode="json"))))


def _canonical_id(namespace: str, value: str) -> tuple[str, str] | None:
    namespace = namespace.casefold().strip().replace("_id", "")
    value = value.strip().casefold()
    if namespace == "doi":
        value = re.sub(r"^(?:https?://(?:dx\.)?doi\.org/|doi:)", "", value)
    elif namespace == "arxiv":
        value = re.sub(r"^(?:arxiv:|https?://arxiv\.org/(?:abs|pdf)/)", "", value)
        value = re.sub(r"\.pdf$", "", value)
        value = re.sub(r"v\d+$", "", value)
    elif namespace == "pubmed":
        namespace = "pmid"
    if not value:
        return None
    return namespace, value


def _ids_from_url(value: str) -> set[tuple[str, str]]:
    parsed = urlparse(unquote(value))
    host = (parsed.hostname or "").casefold()
    path = parsed.path.strip("/")
    if host.endswith("doi.org") and path:
        return {("doi", path.casefold())}
    if host.endswith("arxiv.org"):
        match = re.match(r"(?:abs|pdf)/(.+?)(?:\.pdf)?$", path, re.I)
        if match:
            canonical = _canonical_id("arxiv", match.group(1))
            return {canonical} if canonical else set()
    if host.endswith("pubmed.ncbi.nlm.nih.gov") and path.isdigit():
        return {("pmid", path)}
    return set()
