#!/usr/bin/env python3
"""Evaluate real excerpts using a production workflow and disclosed offline source responses.

No models or network are used. Expected labels are frozen in cases.json and are
never derived from Validator output. See the companion README for limitations.
"""
from __future__ import annotations

import asyncio
from collections import Counter
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "backend/src"))
CASE_DIR = ROOT / "docs/experiments/20261101_validator_metadata_real_cases"
from novelty_agent_framework.agents import DemoCoordinator, DemoPointExtractor, DemoSearchPlanner
from novelty_agent_framework.core import RuntimeDebugConfig
from novelty_agent_framework.persistence import ReferenceStore
from novelty_agent_framework.ports import DoiMetadataRecord, ProviderMetadataResult
from novelty_agent_framework.schemas import (Artifact, ArtifactRole, ContentExtent, Evidence, EvidenceCard,
    EvidenceLocator, EvidenceSource, PaperInput, ResearchTask, TaskResearchResult, TaskResearchStatus, Work, WorkType)
from novelty_agent_framework.schemas.failures import FailureCode
from novelty_agent_framework.tools.metadata_retrieval import MetadataRetrievalService
from novelty_agent_framework.workflows import NoveltyWorkflow, NoveltyWorkflowConfig, NoveltyWorkflowServices


class SingleCoordinator(DemoCoordinator):
    @staticmethod
    def _tasks_for(point, attempt):
        return (ResearchTask(task_id="T-1", novelty_point_id=point.point_id,
            task_type="literature_search", language="en", attempt=attempt,
            description="Find published materials and assess the source quotation and document identity."),)


class ObservedWorkflow(NoveltyWorkflow):
    """Observe the actual input to the built-in Reviewer stage; no model Reviewer."""
    def __init__(self, *args, **kwargs):
        self.observed_reviewer_inputs = []
        super().__init__(*args, **kwargs)

    async def _review_evidence(self, state):
        self.observed_reviewer_inputs.append([card.card_id for card in state.get("validator_accepted_cards", [])])
        return await super()._review_evidence(state)


class RealExcerptResearcher:
    def __init__(self, work, case, output_root, wrong_doi):
        self.work, self.case, self.wrong_doi = work, case, wrong_doi
        self.reference_store = ReferenceStore(output_root)

    async def ainvoke(self, request):
        work, case = self.work, self.case
        text = work["excerpt"]
        quote = text if case != "bad_quote" else text.replace(" ", "_", 1)
        end = len(text) if case != "bad_location" else len(text) - 1
        doi = self.wrong_doi if case == "wrong_doi" else work["doi"]
        doi = None if case in {"no_doi", "both_no_doi", "wrong_page_count"} else doi
        authors = list(work["authors"])
        if case == "wrong_author": authors[0] = "Synthetic Wrong Author"
        year = work["year"] + (1 if case == "wrong_year" else 0)
        count = work["page_count"] + 1 if case == "wrong_page_count" else work["page_count"]
        ids = [{"namespace": "url", "value": work["publisher_url"]}]
        if doi: ids.append({"namespace": "doi", "value": doi})
        paper_id, task, point = request.subject_paper_id, request.research_task, request.novelty_point
        self.reference_store.write_document(paper_id, work_id=work["work_id"], artifact_id="excerpt",
                                            extension="txt", content=text)
        manifest = self.reference_store.load_manifest(paper_id)
        artifact = Artifact(artifact_id="excerpt", work_id=work["work_id"], role=ArtifactRole.EXTRACTED_TEXT,
            media_type="text/plain", relative_path=f"documents/{work['work_id']}/excerpt.txt",
            sha256=hashlib.sha256(text.encode()).hexdigest(), byte_size=len(text.encode()),
            content_extent=ContentExtent.PARTIAL, acquired_at=datetime(2026, 11, 1, tzinfo=timezone.utc))
        actual_work = Work(work_id=work["work_id"], work_type=WorkType.ARTICLE, title=work["title"], authors=authors,
            publication_year=year, venue=work["venue"], publisher=work["publisher"], volume=work["volume"], issue=work["issue"],
            page_count=count, identifiers=ids)
        self.reference_store.persist_manifest(paper_id, manifest.model_copy(update={"works":[actual_work], "artifacts":[artifact]}))
        evidence = Evidence(evidence_id="evidence-1", work_id=work["work_id"], artifact_id="excerpt",
            novelty_point_id=point.point_id, task_id=task.task_id, quote=quote,
            locator=EvidenceLocator(char_start=0, char_end=end), interpretation="quoted published abstract", confidence=1.0)
        source = EvidenceSource(title=work["title"], quote=quote, location=f"artifact excerpt chars:0-{end}",
                                doi=doi, url=work["publisher_url"])
        card = EvidenceCard(card_id="card-1", task_id=task.task_id, novelty_point_id=point.point_id,
            document_title=work["title"], main_contribution="Published abstract excerpt", sources=[source],
            relevance=1.0, confidence=1.0, evidence_ids=["evidence-1"])
        return TaskResearchResult(task_id=task.task_id, novelty_point_id=point.point_id, status=TaskResearchStatus.COMPLETED,
                                  evidence=[evidence], evidence_cards=[card], steps_used=1)


class ControlledProvider:
    def __init__(self, name, work, case, other):
        self.source_id, self.work, self.case, self.other = name, work, case, other
        self.queries, self.closed = [], False
    def retrieve(self, query):
        self.queries.append(query)
        if self.case == "provider_timeout":
            return ProviderMetadataResult(self.source_id, query, "error", "controlled",
                error_type="ReadTimeout", failure_code=FailureCode.PROVIDER_TIMEOUT)
        if self.case == "provider_not_found":
            return ProviderMetadataResult(self.source_id, query, "not_found", "controlled")
        # The wrong DOI is another real Work: a DOI lookup returns that other
        # Work's metadata rather than an impossible mismatched singleton response.
        w = self.other if self.case == "wrong_doi" else self.work
        cloud_doi = None if self.case in {"cloud_no_doi", "both_no_doi"} else w["doi"]
        records = [DoiMetadataRecord(source_id=self.source_id, record_kind="bibliographic", doi=cloud_doi,
            title=w["title"], authors=tuple(w["authors"]), year=w["year"], venue=w["venue"],
            publisher=w["publisher"], volume=w["volume"], issue=w["issue"], page_count=w["page_count"],
            url=w["publisher_url"])]
        if self.case == "source_conflict_or" and self.source_id == "datacite":
            records[0] = DoiMetadataRecord(**{**records[0].__dict__, "year":w["year"]+1})
        if self.case == "unrelated_candidate_or" and self.source_id == "crossref":
            w = self.other
            records.append(DoiMetadataRecord(source_id=self.source_id, record_kind="bibliographic", doi=w["doi"],
                title=w["title"], authors=tuple(w["authors"]), year=w["year"]))
        return ProviderMetadataResult(self.source_id, query, "found", "controlled", tuple(records))
    def close(self): self.closed = True


def run_case(work, other, case, base):
    root = base / (work["work_id"] + "-" + case["name"])
    provider_a = ControlledProvider("crossref", work, case["name"], other)
    provider_b = ControlledProvider("datacite", work, case["name"], other)
    researcher = RealExcerptResearcher(work, case["name"], root, other["doi"])
    cfg = NoveltyWorkflowConfig(max_rounds=1, max_concurrency=1,
        runtime_debug=RuntimeDebugConfig(enabled=False, output_root=root, archive_root=root / "archive"))
    workflow = ObservedWorkflow(NoveltyWorkflowServices(coordinator=SingleCoordinator(), task_researcher=researcher,
        search_planner=DemoSearchPlanner(), point_extractor=DemoPointExtractor()), config=cfg, output_root=root)
    paper = PaperInput(paper_id="evaluation", title="Metadata validation evaluation", full_text="Local evaluation.",
                       claimed_contributions=["Evaluate metadata identity matching."])
    def create_service():
        return MetadataRetrievalService([provider_a, provider_b], owns_providers=True)
    # One async bridge executes synchronous Validator inline for this isolated offline experiment;
    # it does not change the Validator or retrieval semantics and avoids sandbox thread limitations.
    async def inline(fn, *args, **kwargs): return fn(*args, **kwargs)
    def forbidden(*args, **kwargs): raise AssertionError("network forbidden in this offline evaluation")
    with patch("novelty_agent_framework.tools.metadata_retrieval.build_metadata_retrieval_service", create_service), \
         patch("socket.getaddrinfo", forbidden), patch("asyncio.to_thread", inline):
        outcome = workflow.run(paper)
    run = root / "evaluation/recovery" / workflow.last_runtime_run_id
    audit = json.loads((run / "round-1-validator.json").read_text())
    if audit["validator_integrity"].get("checked_card_count") != 1:
        raise RuntimeError(f"{work['work_id']}:{case['name']}: workflow did not supply one card to Validator; errors: {outcome.issues}")
    actual = "accept" if outcome.evidence_cards else "reject"
    events = [{"code": event.code.value, "provider": event.scope.provider,
               "effect": event.conclusion_effect, "message": event.message} for event in outcome.failure_events]
    checked = audit["validator_integrity"]["metadata_authenticity"]["card-1"]
    expected = case["expected_behavior"]
    issues = []
    def require(condition, reason):
        if not condition: issues.append(reason)
    require(actual == case["expect"], f"decision: expected {case['expect']}, got {actual}")
    require(checked["status"] == expected["validator_status"],
            f"validator status: expected {expected['validator_status']}, got {checked['status']}")
    require(len(provider_a.queries) + len(provider_b.queries) == expected["provider_query_count"],
            f"provider query count: expected {expected['provider_query_count']}, got {len(provider_a.queries) + len(provider_b.queries)}")
    queries = {p.source_id: [asdict(query) for query in p.queries] for p in (provider_a, provider_b)}
    if "expected_query_doi" in expected:
        expected_doi = {"none": None, "other_work": other["doi"], "work": work["doi"]}[expected["expected_query_doi"]]
        require(all(len(values) == 1 and values[0]["doi"] == expected_doi for values in queries.values()),
                f"query DOI: expected {expected_doi}, got {queries}")
    require(provider_a.closed and provider_b.closed, "owned provider was not closed")
    require(workflow.observed_reviewer_inputs == [expected["reviewer_input"]],
            f"reviewer stage inputs: expected {[expected['reviewer_input']]}, got {workflow.observed_reviewer_inputs}")
    codes = {event["code"] for event in events}
    for code in expected.get("required_failure_codes", []):
        require(code in codes, f"required error code missing: {code}")
    checks = checked.get("sources", [])
    if expected["provider_query_count"]:
        require(len(checks) == 1, f"expected one source check, got {len(checks)}")
        if len(checks) == 1:
            check = checks[0]
            if "expected_doi_comparison" in expected:
                require(check.get("doi_comparison") == expected["expected_doi_comparison"],
                        f"DOI comparison status differs: {check.get('doi_comparison')}")
            if "expected_query_status" in expected:
                require(check.get("query_status") == expected["expected_query_status"],
                        f"query execution status differs: {check.get('query_status')}")
            retrieval = check.get("retrieval", {})
            if "expected_provider_status" in expected:
                statuses = [provider["status"] for provider in retrieval.get("providers", [])]
                require(statuses == [expected["expected_provider_status"]] * 2,
                        f"provider statuses differ: {statuses}")
            candidates = retrieval.get("cards", [])
            if "minimum_candidates" in expected:
                require(len(candidates) >= expected["minimum_candidates"], f"insufficient candidates: {len(candidates)}")
            if "required_conflict_field" in expected:
                field = expected["required_conflict_field"]
                require(any(field in candidate.get("conflicts", {}) for candidate in candidates), f"missing conflict {field}")
                require(bool(check.get("matched_candidate_id")), "OR result has no matched candidate")
    changes = {
        "baseline": {}, "no_doi": {"local_doi": None},
        "cloud_no_doi": {"provider_doi": None}, "both_no_doi": {"local_doi": None, "provider_doi": None},
        "wrong_doi": {"local_doi": other["doi"], "source_doi": other["doi"],
                      "provider_title": other["title"], "evidence_source_title": work["title"]},
        "wrong_author": {"local_first_author": "Synthetic Wrong Author", "source_first_author": work["authors"][0]},
        "wrong_year": {"local_year": work["year"] + 1, "source_year": work["year"]},
        "wrong_page_count": {"local_doi": None, "local_page_count": (work["page_count"] or 0) + 1,
                             "source_page_count": work["page_count"]},
        "bad_quote": {"quote": work["excerpt"].replace(" ", "_", 1)},
        "bad_location": {"char_end": len(work["excerpt"]) - 1},
        "source_conflict_or": {"datacite_year": work["year"] + 1, "crossref_year": work["year"]},
        "unrelated_candidate_or": {"extra_candidate_doi": other["doi"]},
        "provider_timeout": {"simulated_both_sources": "ReadTimeout"},
        "provider_not_found": {"simulated_both_sources": "not_found"},
    }[case["name"]]
    return {"case_id": f"{work['work_id']}:{case['name']}", "work_id":work["work_id"], **case,
        "perturbation": changes,
        "observed":actual, "reviewer_stage_inputs":workflow.observed_reviewer_inputs,
        "behavior_failures":issues, "policy_expectation_met":not issues,
        "error_events":events, "rejected_reasons":[item.reason for item in outcome.rejected_evidence],
        "metadata_audit":audit["validator_integrity"], "provider_query_count":len(provider_a.queries)+len(provider_b.queries),
        "provider_queries":queries,
        "services_closed":provider_a.closed and provider_b.closed,
        "excerpt_sha256":hashlib.sha256(work["excerpt"].encode()).hexdigest(),
        "input_locator":{"artifact":"excerpt.txt", "char_start":0,
                         "char_end":len(work["excerpt"]) - (1 if case["name"] == "bad_location" else 0)},
        "reference_locator":{"artifact":"excerpt.txt", "char_start":0,
                             "char_end":len(work["excerpt"]), "pdf_page":work["excerpt_pdf_page"]},
        "source_mode":"controlled provider responses derived from publisher/PMC metadata; perturbations synthetic"}


def counts(rows):
    outcome = Counter()
    for row in rows:
        if row["truth"] == "valid":
            outcome["TP" if row["observed"] == "accept" else "FN"] += 1
        elif row["truth"] == "invalid":
            outcome["FP" if row["observed"] == "accept" else "TN"] += 1
        else:
            raise ValueError(f"Unexpected identity truth: {row['truth']}")
    return {key: outcome[key] for key in ("TP", "FP", "TN", "FN")}


def evaluation_group(row):
    if row["name"] in {"bad_quote", "bad_location"}:
        return "local_provenance"
    if row["name"] == "provider_not_found":
        return "mechanism_not_found"
    if row["name"] == "provider_timeout":
        return "mechanism_timeout"
    if row["name"] in {"baseline", "no_doi", "cloud_no_doi", "both_no_doi",
                       "wrong_doi", "wrong_author", "wrong_year", "wrong_page_count",
                       "source_conflict_or", "unrelated_candidate_or"}:
        return "identity"
    raise ValueError(f"Unclassified evaluation case: {row['name']}")


def behavior_counts(rows):
    return {"total": len(rows),
            "met_expectations": sum(row["policy_expectation_met"] for row in rows),
            "failed_expectations": sum(not row["policy_expectation_met"] for row in rows)}


def main():
    started_at = datetime.now(timezone.utc).isoformat()
    works = json.loads((CASE_DIR / "works.json").read_text())["works"]
    cases = json.loads((CASE_DIR / "cases.json").read_text())["templates"]
    for work in works:
        excerpt = (CASE_DIR / work["excerpt_path"]).read_bytes()
        if hashlib.sha256(excerpt).hexdigest() != work["excerpt_sha256"] or excerpt.decode("utf-8") != work["excerpt"]:
            raise ValueError(f"Excerpt hash/content mismatch: {work['work_id']}")
    with tempfile.TemporaryDirectory(prefix="validator-real-cases-") as directory:
        rows = [run_case(work, works[(i + 1) % len(works)], case, Path(directory)) for i, work in enumerate(works) for case in cases
                if case["name"] != "wrong_page_count" or work["page_count"] is not None]
    for row in rows:
        row["evaluation_group"] = evaluation_group(row)
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    source_diff = subprocess.check_output(["git", "diff", "--binary", "HEAD", "--", "backend", "config"], cwd=ROOT)
    untracked = subprocess.check_output(["git", "ls-files", "--others", "--exclude-standard", "-z", "--", "backend", "config"], cwd=ROOT).split(b"\0")
    fingerprint = hashlib.sha256(source_diff)
    for path in sorted(item for item in untracked if item):
        fingerprint.update(b"\0" + path + b"\0" + (ROOT / path.decode()).read_bytes())
    input_hashes = {name: hashlib.sha256((CASE_DIR / name).read_bytes()).hexdigest() for name in ("works.json", "cases.json")}
    input_hashes["evaluator_script"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    work_level = Counter()
    for work in works:
        wr = [r for r in rows if r["work_id"] == work["work_id"] and r["evaluation_group"] == "identity"]
        baseline = next(r for r in wr if r["name"] == "baseline")
        work_level["TP" if baseline["observed"] == "accept" else "FN"] += 1
        invalid = [r for r in wr if r["truth"] == "invalid"]
        work_level["FP" if any(r["observed"] == "accept" for r in invalid) else "TN"] += 1
    groups = {name: [row for row in rows if row["evaluation_group"] == name]
              for name in ("identity", "local_provenance", "mechanism_not_found", "mechanism_timeout")}
    output = {"case_count":len(rows), "work_count":len(works), "started_at_utc":started_at,
              "source_access_date":"2026-11-01 (web-accessed publisher/PMC/PubMed pages, possibly cached)",
              "git_head":commit, "tracked_backend_config_diff_sha256":fingerprint.hexdigest(),
              "backend_config_dirty": bool(source_diff or any(untracked)), "input_sha256":input_hashes,
              "identity_work_level_definition":"Each Work contributes one identity-positive baseline and one grouped identity-negative unit; the negative is FP if any invalid identity perturbation was accepted. The same Work contributes to both units.",
              "identity_work_level_counts":{key:work_level[key] for key in ("TP", "FP", "TN", "FN")},
              "metadata_provenance":"publisher and PubMed/PMC; frozen simulated service responses",
              "labels":"AI agent based on independently inspected sources; not human reviewed", "network":"disabled",
              "models":"none", "identity_case_level_counts":counts(groups["identity"]),
              "identity_by_work_counts":{w["work_id"]:counts([r for r in groups["identity"] if r["work_id"] == w["work_id"]]) for w in works},
              "local_provenance_behavior":behavior_counts(groups["local_provenance"]),
              "mechanism_not_found_behavior":behavior_counts(groups["mechanism_not_found"]),
              "mechanism_timeout_behavior":behavior_counts(groups["mechanism_timeout"]),
              "policy_expectation_failures":[r["case_id"] for r in rows if not r["policy_expectation_met"]], "cases":rows}
    (CASE_DIR / "results.json").write_text(json.dumps(output, ensure_ascii=False, indent=2)+"\n")
    print(json.dumps({k:v for k,v in output.items() if k != "cases"}, ensure_ascii=False, indent=2))

if __name__ == "__main__": main()
