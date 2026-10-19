"""Researcher-facing lexical search over the subject-reference corpus only."""

from __future__ import annotations

import math
import re
import time
import unicodedata
from collections import Counter

from ..persistence import SubjectReferenceStore
from ..schemas import (
    ArtifactHandle,
    ArtifactNamespace,
    ReferenceSearchArguments,
    ReferenceSearchItem,
    ReferenceSearchResult,
    ResearcherToolObservation,
    TaskResearchRequest,
)


class ReferenceSearchTool:
    name = "reference_search"
    description = "在论文作者自带的参考文献池中进行本地关键词检索。"
    args_schema = ReferenceSearchArguments

    def __init__(self, store: SubjectReferenceStore | None = None) -> None:
        self.store = store or SubjectReferenceStore()

    async def ainvoke(self, arguments: ReferenceSearchArguments, *, scope: TaskResearchRequest) -> ResearcherToolObservation:
        started = time.monotonic()
        result = self.search(scope.subject_paper_id, arguments)
        return ResearcherToolObservation(tool_name=self.name, arguments=arguments.model_dump(mode="json"), succeeded=True, summary=f"从原论文参考文献中召回 {len(result.results)} 条", payload={"reference_search_result": result.model_dump(mode="json")}, elapsed_ms=int((time.monotonic() - started) * 1000))

    def search(self, paper_id: str, arguments: ReferenceSearchArguments) -> ReferenceSearchResult:
        arguments = ReferenceSearchArguments.model_validate(arguments)
        manifest = self.store.load_manifest(paper_id)
        ledger = self.store.load_bootstrap(paper_id)
        ref_by_work = {entry.resolved_work_id: entry.reference_id for entry in ledger.entries if entry.resolved_work_id}
        records = {record.work_id: record for record in manifest.source_records if record.work_id}
        artifacts: dict[str, list[ArtifactHandle]] = {}
        for artifact in manifest.artifacts:
            artifacts.setdefault(artifact.work_id, []).append(ArtifactHandle(namespace=ArtifactNamespace.SUBJECT_REFERENCE, artifact_id=artifact.artifact_id))
        documents = []
        for work in manifest.works:
            record = records.get(work.work_id)
            abstract = record.abstract if record else None
            text = " ".join([work.title, *work.authors, str(work.publication_year or ""), abstract or ""])
            documents.append((work, abstract, Counter(_tokens(text))))
        query = Counter(_tokens(arguments.query))
        document_frequency = {
            token: sum(token in terms for _, _, terms in documents)
            for token in query
        }
        scored = []
        for work, abstract, terms in documents:
            score = sum(query[token] * terms[token] * (math.log((len(documents) + 1) / (document_frequency[token] + 1)) + 1.0) for token in query)
            if score > 0:
                scored.append(ReferenceSearchItem(reference_id=ref_by_work.get(work.work_id, f"work:{work.work_id}"), work_id=work.work_id, title=work.title, authors=work.authors, publication_year=work.publication_year, artifact_handles=artifacts.get(work.work_id, []), abstract_preview=(abstract[:500] if abstract else None), score=float(score)))
        scored.sort(key=lambda item: (-item.score, item.title.casefold()))
        return ReferenceSearchResult(query=arguments.query, results=scored[: arguments.max_results])

    def project_model_context(self, observation: ResearcherToolObservation) -> dict[str, object]:
        return {"succeeded": observation.succeeded, "summary": observation.summary, **observation.payload}


def _tokens(value: str) -> list[str]:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    latin = re.findall(r"[a-z0-9]+", normalized)
    chinese = re.findall(r"[\u4e00-\u9fff]", normalized)
    return latin + chinese
