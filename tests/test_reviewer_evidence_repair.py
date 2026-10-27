"""Fixed NP-3 Reviewer boundaries; no model or network service is called."""

from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path

import pytest

from backend.env import ModelResponse, ModelToolCall
from novelty_agent_framework.agents.evidence_reviewer import (
    EvidenceReviewerConfig, NoveltyEvidenceReviewer, _compact_summary_rows, _extract_json, _guard_unsupported_absence,
    _legacy_guard_shadow, _review_failure_cause,
    _validate_review_references, _summary_incomplete_reason, _summary_model_rows,
    _verify_summary_payload,
)
from novelty_agent_framework.persistence import ReferenceStore
from novelty_agent_framework.core import RuntimeArtifactManager, RuntimeDebugConfig
from novelty_agent_framework.core.tool_call_harness import ToolCallHarnessEvent
from novelty_agent_framework.core.tool_call_harness import ToolCallBudgetExhausted
from novelty_agent_framework.schemas import (NoveltyPointReview, NoveltyPointReviewRequest,
    ReviewerCardDraft, ReviewerSummaryDraft, ReaderArguments)
from novelty_agent_framework.tools import ReviewerReaderTool, ResearcherToolRegistry
from novelty_agent_framework.tools.reference_reader import ReferenceArtifactReaderTool
from novelty_agent_framework.tools.renderer import _format_conclusions


ROOT = Path(__file__).resolve().parents[1] / "docs/experiments/20260917_214820_retrieval_repair/runs/full/0001"
PAPER = ROOT / "MF2033k6lC"
STAGE = PAPER / "runtime/run-d606cd37534a47349b905d42c6141768/stages/0012_review_evidence/input.json"
CARD_ID = "card_055dcd710a45194158c1d5ba"
ABSTRACT_ID = "art_082358a7cb0efe62d471f7fb"
BODY_ID = "art_ebc47b0e70f5b363f3d871f2"


def test_reviewer_model_schemas_exclude_system_registered_fields() -> None:
    card = json.dumps(ReviewerCardDraft.model_json_schema(), ensure_ascii=False)
    summary = json.dumps(ReviewerSummaryDraft.model_json_schema(), ensure_ascii=False)
    assert all(name not in card for name in ("read_id", "read_citations", "char_start", "char_end",
                                             "review_evidence", "ReviewEvidence", "incomplete_reason", "exact_quote"))
    assert all(name not in summary for name in ("read_citations", "review_evidence", "ReviewEvidence", "incomplete_reason"))
    with pytest.raises(Exception):
        ReviewerCardDraft.model_validate({"novelty_point_id": "NP-3", "status": "insufficient_evidence",
                                          "review_evidence": []})
    reviewer = NoveltyEvidenceReviewer(_ScriptedClient())
    _, prompt = reviewer._render_point_prompt(fixed_request())
    assert "ReviewerCardDraft" not in prompt or "review_evidence" not in prompt
    assert "ReviewEvidence" not in prompt


def test_budget_exception_chain_is_not_mislabeled_technical() -> None:
    try:
        raise ToolCallBudgetExhausted("reader limit")
    except ToolCallBudgetExhausted as original:
        try:
            raise RuntimeError("finalization failed") from original
        except RuntimeError as wrapped:
            assert _review_failure_cause(wrapped) == "budget_exhausted"
    assert _review_failure_cause(ValueError("invalid read_id")) == "technical_error"


def test_reviewer_budget_finalization_registers_returned_read() -> None:
    request, tool = fixed_request(), reader_tool()
    args = ReaderArguments(artifact_id=BODY_ID, char_start=16000, max_chars=300)
    read = asyncio.run(tool.ainvoke(args, scope=request)).payload["read_result"]
    draft = {"novelty_point_id": "NP-3", "status": "insufficient_evidence",
             "supplement_request": {"reason": "该方法片段不足以核验全部关键特征"}}
    client = _ScriptedClient(
        ModelResponse(content=None, tool_calls=(ModelToolCall(
            id="read-method", name="reader", arguments=args.model_dump()),)),
        ModelResponse(content=json.dumps(draft, ensure_ascii=False)),
    )
    reviewer = NoveltyEvidenceReviewer(client, tool_registry=ResearcherToolRegistry([tool]),
        config=EvidenceReviewerConfig(max_steps=2, max_tool_calls=1, max_total_read_chars=8000))
    result = asyncio.run(reviewer.review_card(request))
    assert result.status.value == "insufficient_evidence"
    assert result.incomplete_reason == "semantic_evidence"
    assert len(result.review_evidence) == 1
    assert result.review_evidence[0].exact_quote == read["text"]
    assert len(client.calls) == 2
    assert client.calls[-1][1].tools == () and client.calls[-1][1].tool_choice == "none"
    assert "cards=[]" not in client.calls[-1][0][-1].content
    assert "read_id" not in client.calls[0][0][0].content


def test_selected_read_quote_reaches_summary_without_repeated_feature_ref() -> None:
    request, tool = fixed_request(), reader_tool()
    args = ReaderArguments(artifact_id=BODY_ID, char_start=16701, max_chars=500)
    read = asyncio.run(tool.ainvoke(args, scope=request)).payload["read_result"]
    draft = {"novelty_point_id": "NP-3", "status": "insufficient_evidence",
             "supplement_request": {"reason": "其他关键特征仍需核验"}}
    client = _ScriptedClient(ModelResponse(content=None, tool_calls=(ModelToolCall(
        id="method", name="reader", arguments=args.model_dump()),)),
        ModelResponse(content=json.dumps(draft, ensure_ascii=False)))
    review = asyncio.run(NoveltyEvidenceReviewer(
        client, tool_registry=ResearcherToolRegistry([tool])).review_card(request))
    rows, valid_ids = _compact_summary_rows(request, [{"index": 0, "card_id": CARD_ID,
        "novelty_point_id": "NP-3", "status": "completed", "review": review.model_dump(mode="json")}])
    new_id = review.review_evidence[0].evidence_id
    assert new_id in valid_ids
    assert any(q["evidence_id"] == new_id and q["quote"] == read["text"]
               for q in rows[0]["key_quotes"])
    assert new_id in json.dumps(_summary_model_rows(rows), ensure_ascii=False)
    summary = {"novelty_point_id": "NP-3", "status": "insufficient_evidence",
               "supplement_request": {"reason": "Synthetic: further comparison required"}}
    summary_client = _ScriptedClient(ModelResponse(content=json.dumps(summary)))
    asyncio.run(NoveltyEvidenceReviewer(summary_client).summarize_reviews(request, [
        {"index": 0, "card_id": CARD_ID, "novelty_point_id": "NP-3",
         "status": "completed", "review": review.model_dump(mode="json")}]))
    sent = summary_client.calls[0][0][1].content
    assert any(q["evidence_id"] == new_id and q["quote"] == read["text"]
               for q in json.loads(sent)["card_reviews"][0]["key_quotes"])
    assert any(item["evidence_id"] == new_id and item["display_status"] == "full"
               for item in _verify_summary_payload(rows, sent))
    broken = json.loads(sent)
    broken["card_reviews"][0]["key_quotes"] = []
    with pytest.raises(ValueError, match="lost selected evidence"):
        _verify_summary_payload(rows, json.dumps(broken))


def test_omitted_quote_is_not_a_summary_citable_reference() -> None:
    request = fixed_request()
    base = request.evidence[0]
    second = base.model_copy(update={"evidence_id": "ev_synthetic_second"})
    long_evidence = base.model_copy(update={"quote": "x" * 12_000})
    card = request.cards[0].model_copy(update={"evidence_ids": [base.evidence_id, second.evidence_id]})
    request = request.model_copy(update={"cards": [card], "evidence": [long_evidence, second]})
    review = NoveltyPointReview(novelty_point_id="NP-3", status="insufficient_evidence",
        highly_relevant_works=[{"work_id": base.work_id, "card_ids": [CARD_ID],
            "evidence_ids": [base.evidence_id, second.evidence_id], "relevance_reason": "synthetic"}])
    rows, ids = _compact_summary_rows(request, [{"index": 0, "card_id": CARD_ID,
        "novelty_point_id": "NP-3", "status": "completed", "review": review.model_dump(mode="json")}])
    quote = next(item for item in rows[0]["key_quotes"] if item["evidence_id"] == second.evidence_id)
    assert quote["display_status"] == "omitted" and quote["displayed_chars"] == 0
    assert second.evidence_id not in ids
    assert base.evidence_id in ids


def test_summary_assembly_survives_synchronized_identity_remap() -> None:
    request = fixed_request()
    old_card, old_evidence = request.cards[0], request.evidence[0]
    new_point = request.novelty_point.model_copy(update={"point_id": "NP-91"})
    new_evidence = old_evidence.model_copy(update={"evidence_id": "ev_remapped_91",
                                           "work_id": "wrk_remapped_91",
                                           "novelty_point_id": "NP-91"})
    new_card = old_card.model_copy(update={"card_id": "card_remapped_91",
                                    "novelty_point_id": "NP-91",
                                    "evidence_ids": ["ev_remapped_91"]})
    request = request.model_copy(update={"subject_paper_id": "paper_remapped_91",
        "novelty_point": new_point,
        "tasks": [task.model_copy(update={"novelty_point_id": "NP-91"}) for task in request.tasks],
        "cards": [new_card], "evidence": [new_evidence]})
    review = NoveltyPointReview(novelty_point_id="NP-91", status="insufficient_evidence",
        highly_relevant_works=[{"work_id": "wrk_remapped_91", "card_ids": ["card_remapped_91"],
            "evidence_ids": ["ev_remapped_91"], "relevance_reason": "synthetic"}])
    rows, ids = _compact_summary_rows(request, [{"index": 7, "card_id": "card_remapped_91",
        "novelty_point_id": "NP-91", "status": "completed", "review": review.model_dump(mode="json")}])
    assert ids == {"ev_remapped_91"}
    assert rows[0]["key_quotes"][0]["quote"] == old_evidence.quote
    assert rows[0]["key_quotes"][0]["semantic_support"] == "unassessed"


def test_reviewer_contract_violation_gets_one_strict_repair() -> None:
    request, tool = fixed_request(), reader_tool()
    illegal = {"novelty_point_id": "NP-3", "status": "insufficient_evidence",
               "review_evidence": [{"evidence_id": "fabricated"}]}
    valid = {"novelty_point_id": "NP-3", "status": "insufficient_evidence",
             "supplement_request": {"reason": "缺少关键方法段"}}
    client = _ScriptedClient(ModelResponse(content=json.dumps(illegal)),
                             ModelResponse(content=json.dumps(valid, ensure_ascii=False)))
    reviewer = NoveltyEvidenceReviewer(client, tool_registry=ResearcherToolRegistry([tool]))
    result = asyncio.run(reviewer.review_card(request))
    assert result.status.value == "insufficient_evidence"
    assert result.review_evidence == []
    assert len(client.calls) == 2
    assert '"review_evidence"' not in client.calls[1][0][-1].content.split('"schema": ', 1)[-1]


def fixed_request() -> NoveltyPointReviewRequest:
    state = json.loads(STAGE.read_text())
    card = next(item for item in state["validator_accepted_cards"] if item["card_id"] == CARD_ID)
    point = next(item for item in state["novelty_points"] if item["point_id"] == "NP-3")
    return NoveltyPointReviewRequest(
        subject_paper_id="MF2033k6lC", novelty_point=point,
        tasks=[item for item in state["all_research_tasks"] if item["novelty_point_id"] == "NP-3"],
        cards=[card], evidence=[item for item in state["raw_evidence"]
                               if item["evidence_id"] in card["evidence_ids"]],
    )


def reader_tool() -> ReviewerReaderTool:
    return ReviewerReaderTool(ReferenceArtifactReaderTool(ReferenceStore(ROOT)))


def test_np3_catalog_and_permission_match_real_manifest() -> None:
    request, tool = fixed_request(), reader_tool()
    catalog = tool.material_catalog(request)
    assert {item["artifact_id"] for item in catalog} == {ABSTRACT_ID, BODY_ID}
    body = next(item for item in catalog if item["artifact_id"] == BODY_ID)
    assert body["content_extent"] == "unknown"
    assert body["section_hints"]
    result = asyncio.run(tool.ainvoke(ReaderArguments(artifact_id=BODY_ID, max_chars=128), scope=request))
    assert result.payload["read_result"]["work_id"] == "wrk_b77f32398f578aa48e68b57c"
    for forbidden in ("art_239d9f203a9c73612f1cbbaa", "missing-artifact"):
        with pytest.raises(PermissionError):
            asyncio.run(tool.ainvoke(ReaderArguments(artifact_id=forbidden), scope=request))


def test_summary_eof_is_not_a_content_read() -> None:
    request, tool = fixed_request(), reader_tool()
    length = next(item["readable_chars"] for item in tool.material_catalog(request)
                  if item["artifact_id"] == ABSTRACT_ID)
    result = asyncio.run(tool.ainvoke(ReaderArguments(
        artifact_id=ABSTRACT_ID, char_start=length, max_chars=100), scope=request))
    assert result.payload["read_status"] == "artifact_eof"
    assert result.payload["read_result"]["text"] == ""
    assert BODY_ID in {item["artifact_id"] for item in result.payload["material_catalog"]}


class _ScriptedClient:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    async def acomplete(self, messages, *, options=None):
        self.calls.append((list(messages), options))
        return self.responses.pop(0)


def test_read_evidence_is_registered_and_carried_to_summary() -> None:
    request, tool = fixed_request(), reader_tool()
    args = ReaderArguments(artifact_id=BODY_ID, char_start=0, max_chars=180)
    read = asyncio.run(tool.ainvoke(args, scope=request)).payload["read_result"]
    from novelty_agent_framework.core.reviewer_evidence import register_read_evidence
    evidence_id = register_read_evidence(read, request, tool.material_catalog(request)).evidence_id
    original_card = request.cards[0].model_dump_json()
    original_evidence = request.evidence[0].model_dump_json()
    output = {
        "novelty_point_id": "NP-3", "status": "reviewed", "verdict": "partially_novel",
        "verdict_reason": "合成测试：仅核验引用传递。", "confidence": 0.5,
        "highly_relevant_works": [{"work_id": request.evidence[0].work_id,
                                   "card_ids": [CARD_ID], "evidence_ids": [evidence_id],
                                   "relevance_reason": "合成关系，不作语义结论。"}],
        "feature_comparisons": [{"feature_id": "F1", "work_id": request.evidence[0].work_id,
                                 "relation": "partially_supported", "basis_type": "grounded_inference",
                                 "evidence_refs": [evidence_id], "reason": "合成关系。",
                                 "source_context": "unknown"}],
    }
    client = _ScriptedClient(ModelResponse(content=None, tool_calls=(
        ModelToolCall(id="read-body", name="reader", arguments=args.model_dump()),)),
        ModelResponse(content=json.dumps(output, ensure_ascii=False)))
    reviewer = NoveltyEvidenceReviewer(client, tool_registry=ResearcherToolRegistry([tool]))
    review = asyncio.run(reviewer.review_card(request))
    assert review.status.value == "reviewed"
    assert len(review.review_evidence) == 1
    new = review.review_evidence[0]
    assert new.exact_quote == read["text"] and new.artifact_id == BODY_ID
    assert new.evidence_id in review.highly_relevant_works[0].evidence_ids
    assert review.feature_comparisons[0].evidence_refs == [new.evidence_id]
    assert request.cards[0].model_dump_json() == original_card
    assert request.evidence[0].model_dump_json() == original_evidence
    rows, ids = _compact_summary_rows(request, [{"index": 1, "card_id": CARD_ID,
        "novelty_point_id": "NP-3", "status": "completed", "review": review.model_dump(mode="json")}])
    assert new.evidence_id in ids
    assert rows[0]["key_quotes"][0]["quote"] == read["text"]
    assert rows[0]["summary_input_complete"] is True
    assert _legacy_guard_shadow(review, request)["would_change"] is False
    summary_output = review.model_dump(mode="json")
    for system_field in ("read_citations", "review_evidence", "incomplete_reason", "execution_issues", "reader_observations"):
        summary_output.pop(system_field)
    summary_client = _ScriptedClient(ModelResponse(content=json.dumps(summary_output, ensure_ascii=False)))
    summarizer = NoveltyEvidenceReviewer(summary_client, tool_registry=ResearcherToolRegistry([tool]))
    final = asyncio.run(summarizer.summarize_reviews(request, [{"index": 1, "card_id": CARD_ID,
        "novelty_point_id": "NP-3", "status": "completed", "review": review.model_dump(mode="json")}]))
    assert final.review_evidence[0].evidence_id == new.evidence_id
    sent = json.loads(summary_client.calls[0][0][1].content)["card_reviews"][0]
    assert sent["key_quotes"][0]["quote"] == read["text"]
    assert "review_evidence" not in sent and "review_evidence" not in sent["review"]
    displayed = _format_conclusions([{"novelty_point_id": "NP-3", "review_status": "reviewed",
        "verdict": "partially_novel", "review_evidence": [new.model_dump(mode="json")]}],
        {"NP-3": request.novelty_point.model_dump(mode="json")})
    assert new.evidence_id in displayed and new.exact_quote in displayed


def test_unknown_or_wrong_work_incremental_reference_is_rejected() -> None:
    request = fixed_request()
    review = NoveltyPointReview(novelty_point_id="NP-3", status="reviewed",
        verdict="partially_novel", verdict_reason="test", confidence=0.5,
        highly_relevant_works=[{"work_id": request.evidence[0].work_id,
            "card_ids": [CARD_ID], "evidence_ids": ["made-up"],
            "relevance_reason": "test"}])
    with pytest.raises(ValueError, match="unknown evidence_id"):
        _validate_review_references(review, request)
    wrong_data = review.model_dump()
    wrong_data["highly_relevant_works"] = []
    wrong_data["feature_comparisons"] = [{"feature_id": "F1", "work_id": "other-work",
        "relation": "unknown", "basis_type": "insufficient",
        "reason": "synthetic", "evidence_refs": []}]
    wrong_work = NoveltyPointReview.model_validate(wrong_data)
    with pytest.raises(ValueError, match="outside request scope"):
        _validate_review_references(wrong_work, request)


def test_empty_or_failed_observation_registers_no_evidence() -> None:
    from novelty_agent_framework.core.reviewer_evidence import ReviewerEvidenceRegistry
    request, tool = fixed_request(), reader_tool()
    observation = asyncio.run(tool.ainvoke(ReaderArguments(
        artifact_id=BODY_ID, max_chars=40), scope=request))
    registry = ReviewerEvidenceRegistry(request, tool.material_catalog(request))
    failed = observation.model_copy(update={"succeeded": False})
    assert registry.project_model_context(failed, {})["evidence"] == []
    empty = dict(observation.payload["read_result"], text="", char_start=0, char_end=0)
    assert registry.project_model_context(observation.model_copy(
        update={"payload": {"read_result": empty}}), {})["evidence"] == []
    assert registry.evidence == {}


def test_registered_quote_preserves_whitespace_at_exact_character_range() -> None:
    request, tool = fixed_request(), reader_tool()
    content = (PAPER / "references/documents/wrk_b77f32398f578aa48e68b57c" /
               f"{BODY_ID}.txt").read_text()
    start = content.index("\n")
    observation = asyncio.run(tool.ainvoke(ReaderArguments(
        artifact_id=BODY_ID, char_start=start, max_chars=20), scope=request))
    read = observation.payload["read_result"]
    from novelty_agent_framework.core.reviewer_evidence import register_read_evidence
    evidence = register_read_evidence(read, request, tool.material_catalog(request))
    assert evidence.exact_quote == read["text"]
    assert len(evidence.exact_quote) == evidence.char_end - evidence.char_start


def test_summary_preserves_technical_or_budget_incomplete_reason() -> None:
    assert _summary_incomplete_reason([{"status": "failed"}]) == "technical_error"
    assert _summary_incomplete_reason([{"status": "completed", "review": {
        "incomplete_reason": "budget_exhausted"}}]) == "budget_exhausted"
    text = _format_conclusions([{"novelty_point_id": "NP-3",
        "review_status": "insufficient_evidence", "incomplete_reason": "technical_error"}], {})
    assert "核验未完成，无法裁定" in text


def test_reviewer_debug_export_contains_read_lineage_and_material(tmp_path: Path) -> None:
    output_root = tmp_path / "outputs"
    shutil.copytree(PAPER / "references", output_root / "MF2033k6lC/references")
    request = fixed_request()
    tool = ReviewerReaderTool(ReferenceArtifactReaderTool(ReferenceStore(output_root)))
    args = ReaderArguments(artifact_id=BODY_ID, max_chars=64)
    read = asyncio.run(tool.ainvoke(args, scope=request)).payload["read_result"]
    from novelty_agent_framework.core.reviewer_evidence import register_read_evidence
    evidence_id = register_read_evidence(read, request, tool.material_catalog(request)).evidence_id
    body = {"novelty_point_id": "NP-3", "status": "reviewed",
        "verdict": "partially_novel", "verdict_reason": "synthetic trace", "confidence": 0.5,
        "highly_relevant_works": [{"work_id": read["work_id"], "card_ids": [CARD_ID],
                                    "evidence_ids": [evidence_id], "relevance_reason": "synthetic"}]}
    client = _ScriptedClient(ModelResponse(content=None, tool_calls=(
        ModelToolCall(id="debug-read", name="reader", arguments=args.model_dump()),)),
        ModelResponse(content=json.dumps(body)))
    manager = RuntimeArtifactManager("MF2033k6lC", config=RuntimeDebugConfig(
        output_root=output_root, archive_root=tmp_path / "archive", max_inline_bytes=100))
    manager.activate()
    stage = manager.start_stage("review_evidence", {})
    try:
        review = asyncio.run(NoveltyEvidenceReviewer(
            client, tool_registry=ResearcherToolRegistry([tool])).review_card(request))
        manager.finish_stage(stage, {"review": review.model_dump(mode="json")})
        _, archive = manager.finish_run("SUCCESS")
    finally:
        manager.deactivate()
    events = [json.loads(path.read_text()) for path in (archive / "reviewer_events").glob("*.json")]
    assert any(item["phase"] == "reviewer_case_start" for item in events)
    event = next(item for item in events if item["phase"] == "single_card_result")
    assert event["phase"] == "single_card_result"
    assert event["review"]["review_evidence"][0]["read_id"] == read["read_id"]
    assert event["read_registration"][0]["status"] == "validated"
    assert event["legacy_guard_shadow"]["formal_status"] == "reviewed"
    raw_output = event["raw_model_output"]
    assert raw_output["type"] == "content_reference"
    assert (archive / raw_output["path"]).is_file()
    archived_body = archive / "workspace/MF2033k6lC/references/documents" / read["work_id"] / f"{BODY_ID}.txt"
    assert archived_body.is_file()
    assert archived_body.read_bytes() == (PAPER / "references/documents" / read["work_id"] /
                                          f"{BODY_ID}.txt").read_bytes()


@pytest.mark.parametrize("call_number,card_id", [
    ("0069", CARD_ID), ("0071", "card_927caf724042beff967b2231"),
])
def test_archived_raw_review_g0_vs_g1_is_only_a_rule_comparison(call_number, card_id) -> None:
    state = json.loads(STAGE.read_text())
    card = next(item for item in state["validator_accepted_cards"] if item["card_id"] == card_id)
    point = next(item for item in state["novelty_points"] if item["point_id"] == "NP-3")
    request = NoveltyPointReviewRequest(subject_paper_id="MF2033k6lC", novelty_point=point,
        cards=[card], evidence=[item for item in state["raw_evidence"]
                               if item["evidence_id"] in card["evidence_ids"]])
    llm = next((PAPER / "runtime/run-d606cd37534a47349b905d42c6141768/llm_calls").glob(
        call_number + "_*.json"))
    raw = NoveltyPointReview.model_validate_json(_extract_json(
        json.loads(llm.read_text())["response"]["content"]))
    before = raw.model_dump_json()
    assert _validate_review_references(raw, request).status.value == "reviewed"
    assert _guard_unsupported_absence(raw, request).status.value == "insufficient_evidence"
    assert _legacy_guard_shadow(raw, request)["would_change"] is True
    assert raw.model_dump_json() == before
