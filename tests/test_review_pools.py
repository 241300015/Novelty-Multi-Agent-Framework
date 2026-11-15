"""Offline regressions for discard routing, gate isolation and sealed report sources."""
import asyncio
from copy import deepcopy
import json
from pathlib import Path

import pytest

from novelty_agent_framework.core.integrity_gates import validate_review_integrity, validate_report_integrity
from novelty_agent_framework.core.report_binding import assemble_report
from novelty_agent_framework.core.report_adoption import report_evidence_cards
from novelty_agent_framework.core.review_pools import pool_payloads
from novelty_agent_framework.persistence import ReferenceStore
from novelty_agent_framework.schemas import NoveltyPointReview, ReportNarrativeDraft
from test_reviewer_summary_checkpoint import setup_case


def gate_case(tmp_path, *, discard=False, pending=False):
    request, _, _, rows, path = setup_case(tmp_path, incremental=True)
    review = NoveltyPointReview.model_validate(rows[0]["review"])
    cid = request.cards[0].card_id
    review.summary_harness_state = {"stage": "select_judgments" if pending else "complete",
        "adopted_card_ids": [] if discard or pending else [cid],
        "discarded_card_ids": [cid] if discard else []}
    if discard:
        review.highly_relevant_works = []
        review.feature_comparisons = []
    if pending:
        review.incomplete_reason = "technical_error"
    kwargs = dict(evidence_cards=request.cards, evidence=request.evidence,
        tasks=request.tasks, novelty_points=[request.novelty_point],
        paper_id=request.subject_paper_id, reference_store=ReferenceStore(tmp_path), card_reviews=rows)
    return request, review, rows, path, kwargs


def assembled(request, reviews, rows):
    draft = ReportNarrativeDraft.model_validate({"conclusions": [{
        "novelty_point_id": request.novelty_point.point_id, "summary": "仅限现有证据。"}]})
    return assemble_report(draft, paper_id=request.subject_paper_id, novelty_reviews=reviews,
        novelty_points=[request.novelty_point], evidence_cards=request.cards,
        evidence=request.evidence, card_reviews=rows)


@pytest.mark.parametrize("damage", ["missing", "hash"])
@pytest.mark.parametrize("discard", [False, True])
def test_gate_only_checks_candidate_pool_and_never_revives_rejected_rows(tmp_path, damage, discard):
    request, review, rows, path, kwargs = gate_case(tmp_path, discard=discard)
    if damage == "missing":
        path.unlink()
    else:
        path.write_text("corrupted")
    before = review.model_dump(mode="json"), deepcopy(rows)
    result = validate_review_integrity([review], **kwargs)
    formal = result.reviews[0]
    assert result.audit()["validation_passed"] is discard
    assert formal.report_disposition.gate_status == ("passed" if discard else "rejected")
    assert formal.report_disposition.discarded_card_ids == ([request.cards[0].card_id] if discard else [])
    assert not report_evidence_cards(request.cards, result.reviews)
    report = assembled(request, result.reviews, rows)
    assert not report.sources.cards and not report.sources.card_reviews
    assert not report.partial_card_reviews
    assert not formal.review_evidence and not formal.reader_observations
    assert (review.model_dump(mode="json"), rows) == before
    candidates, discarded = pool_payloads(result.reviews, request.cards, rows)
    assert bool(discarded["card_reviews"]) is discard
    assert bool(candidates["card_reviews"]) is not discard


def test_malformed_discarded_card_is_persistable_but_not_validated(tmp_path):
    request, review, rows, _, kwargs = gate_case(tmp_path, discard=True)
    review.review_evidence = []; review.reader_observations = []; review.read_citations = []
    rows[0]["review"] = {"unparseable": "retained original payload"}
    result = validate_review_integrity([review], **kwargs)
    assert result.audit()["validation_passed"], result.rejected
    assert pool_payloads(result.reviews, request.cards, rows)[1]["card_reviews"] == rows


def test_failed_summary_partial_results_must_pass_gate_a(tmp_path):
    request, review, rows, _, kwargs = gate_case(tmp_path, pending=True)
    # Failed summary itself has no facts. Card-result validation is still required.
    review.review_evidence = []; review.reader_observations = []; review.read_citations = []
    review.highly_relevant_works = []
    passed = validate_review_integrity([review], **kwargs)
    assert passed.audit()["validation_passed"], passed.rejected
    report = assembled(request, passed.reviews, rows)
    assert len(report.partial_card_reviews) == 1
    rows[0]["review"]["reader_observations"][0]["text_sha256"] = "forged"
    rejected = validate_review_integrity([review], **kwargs)
    assert rejected.reviews[0].report_disposition.gate_status == "rejected"
    assert not assembled(request, rejected.reviews, rows).partial_card_reviews


@pytest.mark.parametrize("damage", ["overlap", "foreign", "duplicate"])
def test_invalid_selection_fails_closed(tmp_path, damage):
    request, review, rows, _, kwargs = gate_case(tmp_path)
    cid = request.cards[0].card_id
    if damage == "overlap":
        review.summary_harness_state["discarded_card_ids"] = [cid]
    else:
        review.summary_harness_state["adopted_card_ids"] = [cid, cid if damage == "duplicate" else "foreign"]
    result = validate_review_integrity([review], **kwargs)
    assert result.reviews[0].report_disposition.gate_status == "rejected"
    assert not assembled(request, result.reviews, rows).sources.cards


def test_shared_work_does_not_reintroduce_discarded_card(tmp_path):
    request, review, rows, _, kwargs = gate_case(tmp_path)
    other = request.cards[0].model_copy(update={"card_id": "discarded-same-work"})
    request.cards.append(other)
    review.summary_harness_state["discarded_card_ids"] = [other.card_id]
    rows.append({"card_id": other.card_id, "novelty_point_id": other.novelty_point_id,
                 "status": "completed", "review": {"invalid": "DO_NOT_RESTORE"}})
    result = validate_review_integrity([review], **kwargs)
    assert result.audit()["validation_passed"], result.rejected
    report = assembled(request, result.reviews, rows)
    assert [c.card_id for c in report.sources.cards] == [request.cards[0].card_id]
    assert "DO_NOT_RESTORE" not in report.model_dump_json()
    assert len(pool_payloads(result.reviews, request.cards, rows)[1]["cards"]) == 1


def test_gate_b_audits_snapshot_drift(tmp_path):
    request, review, rows, _, kwargs = gate_case(tmp_path)
    result = validate_review_integrity([review], **kwargs)
    report = assembled(request, result.reviews, rows)
    args = {k: v for k, v in kwargs.items() if k not in {"paper_id", "tasks"}}
    args["novelty_reviews"] = result.reviews
    assert validate_report_integrity(report, **args).validation_passed
    report.sources.card_reviews[0].review.highly_relevant_works[0].relevance_reason = "changed"
    assert any("card results differ" in e for e in validate_report_integrity(report, **args).issues)
    assert rows[0]["review"]["highly_relevant_works"][0]["relevance_reason"] != "changed"
    rows[0]["review"] = {"malformed": "post-gate drift"}
    audit = validate_report_integrity(report, **args)
    assert not audit.validation_passed
    assert any("source reconstruction failed" in e for e in audit.issues)


@pytest.mark.parametrize("reviewed", [True, False])
def test_both_renderers_read_snapshot_and_ignore_legacy_rows(tmp_path, reviewed):
    from test_renderer_conclusion_details import seed_multi_workspace
    from novelty_agent_framework.tools.renderer import _load_workspace_data, _build_markdown_context
    from novelty_agent_framework.tools.renderer_sections import build_stage_context
    workspace = seed_multi_workspace(tmp_path)
    data = _load_workspace_data(workspace)
    cards = deepcopy(data["evidence"]["accepted_evidence_cards"][:1])
    rows = deepcopy(data["reviews"]["card_reviews"][:1])
    conclusion = data["report"]["conclusions"][0]
    conclusion["review_status"] = "reviewed" if reviewed else "insufficient_evidence"
    for field in ("highly_relevant_works", "feature_comparisons"):
        conclusion[field] = conclusion[field][:1]
    conclusion["report_disposition"] = {"selection_status": "complete", "gate_status": "passed",
        "candidate_card_ids": ["C11"], "discarded_card_ids": ["C12"]}
    data["report"]["conclusions"] = [conclusion]
    data["report"]["sources"] = {"schema_version": 1, "cards": cards,
        "card_reviews": rows, "card_work_ids": {"C11": ["work-11"]}}
    data["reviews"]["card_reviews"][0]["review"]["support_reason"] = "POISON_LEGACY"
    data["report"]["partial_card_reviews"] = deepcopy(data["reviews"]["card_reviews"])
    data["evidence"]["accepted_evidence_cards"][0]["main_contribution"] = "POISON_RAW_CARD"
    before = deepcopy(data)
    context = _build_markdown_context("renderer-paper", data)
    stage = build_stage_context(data, workspace, workspace / "report/test.md")
    assert "POISON" not in json.dumps([context, stage], ensure_ascii=False)
    assert "摘要可用于有限比较" in context["concise_conclusions"]
    assert "Paper 12" not in context["concise_literature"]
    assert data == before


def test_historical_version_one_index_is_read_without_rewriting(tmp_path):
    from test_renderer import seed_workspace
    from novelty_agent_framework.report_inputs import _DEPENDENCIES, load_verified_report_inputs
    seed_workspace(tmp_path)
    workspace = tmp_path / "renderer-paper"
    path = workspace / "report-inputs.json"
    index = json.loads(path.read_text())
    index["schema_version"] = 1
    for relative, entry in index["artifacts"].items():
        if entry:
            entry["dependencies"] = {k: v for k, v in entry["dependencies"].items()
                                      if k in _DEPENDENCIES.get(relative, ())}
    path.write_text(json.dumps(index))
    before = path.read_bytes()
    version, _ = load_verified_report_inputs(workspace)
    assert version["schema_version"] == 1 and path.read_bytes() == before


@pytest.mark.parametrize("discard", [False, True])
def test_persisted_pipeline_has_separate_pools_and_nonblocking_gate_b(tmp_path, discard):
    from novelty_agent_framework.persistence import (
        persist_workflow_input, persist_novelty_points, persist_retrieval_plans,
        persist_evidence_cards, persist_reviewer_artifact)
    from novelty_agent_framework.schemas import PaperInput, NoveltyBrief
    from novelty_agent_framework.workflows import NoveltyWorkflow
    from novelty_agent_framework.report_inputs import load_verified_report_inputs, ReportInputVersionError
    request, review, rows, path, _ = gate_case(tmp_path, discard=discard)
    paper = PaperInput(paper_id=request.subject_paper_id, title="Pool regression", full_text="fixture")
    persist_workflow_input(paper, output_root=tmp_path, run_id="pool-run")
    persist_novelty_points(paper, [request.novelty_point], output_root=tmp_path, run_id="pool-run")
    persist_retrieval_plans(paper, request.tasks, rounds=1, output_root=tmp_path, run_id="pool-run")
    persist_evidence_cards(paper, raw_cards=request.cards, accepted_cards=request.cards,
        rejected_evidence=[], output_root=tmp_path, run_id="pool-run")
    persist_reviewer_artifact(paper, "card-reviews", {"card_reviews": rows},
        output_root=tmp_path, run_id="pool-run")
    persist_reviewer_artifact(paper, "point-reviews", {"reviews": [review.model_dump(mode="json")]},
        output_root=tmp_path, run_id="pool-run")
    workspace = tmp_path / paper.paper_id
    raw = (workspace / "reviewer/point-reviews.json").read_bytes()
    workflow = NoveltyWorkflow.default(output_root=tmp_path)
    state = dict(paper=paper, brief=NoveltyBrief(novelty_points=[request.novelty_point],
        paper_summary="fixture"),
        novelty_points=[request.novelty_point], all_research_tasks=request.tasks,
        evidence_cards=request.cards, raw_evidence=request.evidence,
        novelty_reviews=[review], review_card_results=rows, run_id="pool-run")
    async def run():
        state.update(await workflow._validate_synthesis_input(state))
        state.update(await workflow._synthesize_report(state))
        # Gate B must record an authority mismatch without preventing either output.
        state["report"].conclusions[0].summary_harness_state = {"test_drift": True}
        state.update(await workflow._validate_report_integrity(state))
        await workflow._persist_report(state)
        return await workflow._render_report(state)
    rendered = asyncio.run(run())
    candidates = json.loads((workspace / "reviewer/candidates.json").read_text())
    discarded = json.loads((workspace / "reviewer/discarded.json").read_text())
    formal = json.loads((workspace / "novelty-reviews.json").read_text())
    assert "card_reviews" not in formal
    assert bool(discarded["card_reviews"]) is discard
    assert bool(candidates["card_reviews"]) is not discard
    assert (workspace / "reviewer/point-reviews.json").read_bytes() == raw
    assert not state["report_integrity"]["validation_passed"]
    audit = json.loads((workspace / "integrity/gate-b.json").read_text())
    assert audit["report_sha256"] and not audit["validation_passed"]
    for path in rendered["rendered_report_paths"].values():
        assert Path(path).is_file()
    assert "未通过（仅审计，不阻断）" in Path(rendered["rendered_report_paths"]["default"]).read_text()
    version, _ = load_verified_report_inputs(workspace)
    assert version["schema_version"] == 2
    # Re-registering a changed raw stage still invalidates its dependent pools/report.
    persist_reviewer_artifact(paper, "point-reviews", {"reviews": []},
        output_root=tmp_path, run_id="pool-run")
    from novelty_agent_framework.report_inputs import finalize_report_inputs
    finalize_report_inputs(workspace, run_id="pool-run")
    with pytest.raises(ReportInputVersionError, match="依赖版本已变化"):
        load_verified_report_inputs(workspace)


def test_normal_coordinator_workflow_assembles_once(tmp_path, monkeypatch):
    from backend.env import ModelResponse
    from novelty_agent_framework.agents import NoveltyCoordinatorAgent
    from novelty_agent_framework.schemas import NoveltyBrief, PaperInput
    from novelty_agent_framework.workflows import NoveltyWorkflow
    from test_report_narrative_contract import _Client
    from dataclasses import replace
    import novelty_agent_framework.core.report_binding as binding
    request, review, rows, _, kwargs = gate_case(tmp_path)
    reviews = validate_review_integrity([review], **kwargs).reviews
    client = _Client([ModelResponse(content=json.dumps({"conclusions": [{
        "novelty_point_id": request.novelty_point.point_id, "summary": "局部比较。"}]}))])
    coordinator = NoveltyCoordinatorAgent(client)
    workflow = NoveltyWorkflow.default(output_root=tmp_path)
    workflow.services = replace(workflow.services, coordinator=coordinator)
    original = binding.assemble_report_from_draft
    calls = []
    def counted(*args, **kw):
        calls.append(1)
        return original(*args, **kw)
    monkeypatch.setattr(binding, "assemble_report_from_draft", counted)
    monkeypatch.setattr(coordinator, "synthesize", lambda *a, **k: pytest.fail("legacy path called"))
    state = dict(paper=PaperInput(paper_id=request.subject_paper_id, title="test", full_text="fixture"),
        brief=NoveltyBrief(novelty_points=[request.novelty_point], paper_summary="fixture"),
        novelty_points=[request.novelty_point], novelty_reviews=reviews,
        evidence_cards=request.cards, raw_evidence=request.evidence, review_card_results=rows)
    result = asyncio.run(workflow._synthesize_report(state))
    assert len(calls) == 1 and result["report"].sources is not None


@pytest.mark.parametrize("discard", [False, True])
def test_native_support_rows_and_empty_eof_receipts_follow_pool_ownership(tmp_path, discard):
    import hashlib
    from novelty_agent_framework.schemas.domain import ReviewerSupportReview, ReviewerReadAudit
    request, review, rows, path, kwargs = gate_case(tmp_path, discard=discard)
    raw = rows[0]["review"]
    support = ReviewerSupportReview(novelty_point_id=review.novelty_point_id,
        status="reviewed", support_status="usable", support_reason="local fixture",
        harness_state={"stage": "complete"}, highly_relevant_works=raw["highly_relevant_works"],
        review_evidence=raw["review_evidence"], reader_observations=raw["reader_observations"])
    read = kwargs["reference_store"].read_document_slice(request.subject_paper_id,
        artifact_id=review.review_evidence[0].artifact_id, char_start=len(path.read_text()), max_chars=1)
    audit = ReviewerReadAudit(read_id=read.read_id, namespace=read.namespace.value,
        work_id=read.work_id, artifact_id=read.artifact_id, artifact_hash=read.sha256,
        char_start=read.char_start, char_end=read.char_end,
        text_sha256=hashlib.sha256(read.text.encode()).hexdigest())
    support.reader_observations.append(audit)
    review.reader_observations.append(audit)
    rows[0]["review"] = support.model_dump(mode="json")
    if discard:
        path.unlink()
    result = validate_review_integrity([review], **kwargs)
    assert result.audit()["validation_passed"], result.rejected
    report = assembled(request, result.reviews, rows)
    if discard:
        assert not result.reviews[0].reader_observations and not report.sources.card_reviews
    else:
        assert report.sources.card_reviews[0].review.kind == "card_support"
        assert len(report.sources.card_reviews[0].review.reader_observations) == 2
