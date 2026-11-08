"""Offline controlled Reviewer contracts; clients select menus without external I/O."""
import asyncio
import hashlib
import json
from dataclasses import replace

import pytest
from backend.env import ModelResponse, ModelToolCall, PromptLibrary

from novelty_agent_framework.agents.evidence_reviewer import (
    EvidenceReviewerConfig, NoveltyEvidenceReviewer, _compact_summary_rows, _summary_model_rows,
)
from novelty_agent_framework.core.reviewer_evidence import ReviewerEvidenceLedger
from novelty_agent_framework.schemas import ReferenceReadResult, ReviewerSupportDraft, ReviewerSupportReview
from novelty_agent_framework.schemas.domain import parse_card_review, PartialCardReview
from novelty_agent_framework.tools import ReviewerReaderTool, ResearcherToolRegistry
from novelty_agent_framework.workflows.reviewer_reading_policy import ReviewerReadingPolicy
from test_novelty_point_reviewer import _request, RecordingReader, _review_json


class ChoosingClient:
    def __init__(self, *, read=True, draft=None, inject=None):
        self.read, self.draft, self.inject = read, draft, inject
        self.calls = []

    async def acomplete(self, messages, *, options=None):
        await asyncio.sleep(0)  # Force interleaving for shared-instance concurrency tests.
        context = next(json.loads(m.content) for m in reversed(messages)
                       if m.content and m.content.startswith('{"state":'))
        self.calls.append((messages, options, context))
        if self.inject:
            response = await self.inject(self, context)
            if response is not None:
                return response
        stage = context["state"]["stage"]
        if stage != "draft":
            menu = json.loads(messages[-1].content.split("\n")[1])
            if stage in {"assess_card", "reassess_card"}:
                choice = next(key for key, label in menu.items() if label == "部分可用")
            else:
                choice = next((key for key, label in menu.items() if label.startswith("读取")), next(iter(menu))) \
                    if self.read else next(iter(menu))
            return ModelResponse(content=json.dumps({"option_id": choice}))
        source = json.loads(messages[1].content)
        point, card = source["novelty_point"]["point_id"], source["cards"][0]["card_id"]
        eid, info = next(reversed(context["evidence_choices"].items()))
        draft = {"novelty_point_id": point, "support_reason": "当前片段仅支持有限事实，其他特征待核验。",
            "highly_relevant_works": [{"work_id": info["work_id"], "card_ids": [card],
                                       "evidence_ids": [eid], "relevance_reason": "有限事实"}]}
        if self.draft:
            draft.update(self.draft)
        return ModelResponse(content=json.dumps(draft))


def reviewer(client, reader=None, **config):
    return NoveltyEvidenceReviewer(client, config=EvidenceReviewerConfig(**config),
        tool_registry=ResearcherToolRegistry([ReviewerReaderTool(reader or RecordingReader())]))


def row(result, request=None):
    request = request or _request()
    return {"index": 1, "card_id": request.cards[0].card_id, "novelty_point_id": request.novelty_point.point_id,
            "status": "completed", "review": result.model_dump(mode="json")}


def test_skip_is_three_calls_no_reads_and_no_point_verdict():
    client, reader = ChoosingClient(read=False), RecordingReader()
    result = asyncio.run(reviewer(client, reader).review_card(_request()))
    assert result.status.value == "reviewed"
    assert result.support_status.value == "partially_usable"
    assert result.harness_state["transitions"] == ["assess_card", "decide_reading", "draft", "complete"]
    assert len(client.calls) == 3 and not reader.requests and not result.reader_observations
    assert [call[1].response_format["json_schema"]["name"] for call in client.calls] == [
        "harness_decision", "harness_decision", "reviewer_support_draft"]
    final_schema = client.calls[-1][1].response_format["json_schema"]["schema"]
    assert "option_id" not in final_schema["properties"]
    assert "support_reason" in final_schema["required"]
    assert "verdict" not in result.model_dump() and "confidence" not in result.model_dump()
    assert parse_card_review(result.model_dump()) == result
    assert parse_card_review(json.loads(_review_json())).verdict.value == "partially_novel"


def test_read_registered_before_reassessment_and_carried_to_summary():
    client = ChoosingClient()
    result = asyncio.run(reviewer(client).review_card(_request()))
    assert result.status.value == "reviewed", result
    assert [c[2]["state"]["stage"] for c in client.calls] == ["assess_card", "decide_reading", "reassess_card", "draft"]
    item = result.review_evidence[0]
    assert item.exact_quote == "source text" and item.origin_card_id == "C-1"
    assert item.evidence_id in client.calls[2][2]["evidence_choices"]
    assert "read_id" not in client.calls[2][0][-2].content
    rows, ids = _compact_summary_rows(_request(), [row(result)])
    assert item.evidence_id in ids
    assert _summary_model_rows(rows)[0]["key_quotes"][0]["quote"] == "source text"
    assert PartialCardReview(card_id="C-1", novelty_point_id="NP-1", review=result,
                             summary_status="failed").review == result


def test_downloaded_catalog_is_not_a_read():
    request = _request()
    registry = ResearcherToolRegistry([ReviewerReaderTool(RecordingReader())])
    catalog = registry.get("reader").material_catalog(request)
    catalog[0].update(role="extracted_text", content_extent="full_text", readable_chars=11)
    policy = ReviewerReadingPolicy(registry, request, catalog=catalog, max_read_chars=100, max_tool_calls=1)
    assert not policy.read_history and not policy.ledger.evidence and not policy.ledger.audits
    assert "artifact_read_complete" not in policy.snapshot()


def test_menu_distinguishes_complete_abstract_from_body_and_prefers_body():
    request = _request()
    registry = ResearcherToolRegistry([ReviewerReaderTool(RecordingReader())])
    abstract = {**registry.get("reader").material_catalog(request)[0], "role": "abstract", "content_extent": "full"}
    body = {**abstract, "artifact_id": "body-1", "role": "extracted_text"}
    policy = ReviewerReadingPolicy(registry, request, catalog=[abstract, body], max_read_chars=200, max_tool_calls=2)
    policy.resolve(policy.selection({"option_id": next(iter(policy.choices()))}, "assess"))
    labels = list(policy.choices().values())
    assert "body-1" in labels[1] and "类型=extracted_text" in labels[1]
    assert "类型=abstract" in labels[2] and "范围=full" in labels[2]
    assert not policy.ledger.audits


def test_guided_draft_requires_citations_for_non_unknown_relations():
    branches = ReviewerSupportDraft.model_json_schema()["$defs"]["FeatureComparison"]["anyOf"]
    unknown, grounded = branches
    assert unknown["properties"]["relation"]["const"] == "unknown"
    assert "evidence_refs" not in unknown["required"]
    assert set(grounded["properties"]["relation"]["enum"]) == {"supported", "partially_supported", "contradicted"}
    assert "evidence_refs" in grounded["required"]
    assert grounded["properties"]["evidence_refs"]["minItems"] == 1


def test_local_acceptance_fixture_has_production_source_closure(tmp_path):
    from scripts.run_reviewer_controlled_local import fixture
    from novelty_agent_framework.persistence import ReferenceStore
    from novelty_agent_framework.agents.reviewer_checkpoint import _source_snapshot
    request = fixture(ReferenceStore(tmp_path), "preflight", complete=True)
    snapshot = _source_snapshot(request, [], tmp_path)
    assert "research_reference:abstract-preflight" in snapshot


@pytest.mark.parametrize("budget", [1, 2, 3])
def test_model_budget_cannot_skip_required_stages_and_retains_reads(budget):
    client = ChoosingClient()
    result = asyncio.run(reviewer(client, max_steps=budget).review_card(_request()))
    assert len(client.calls) == budget
    assert result.incomplete_reason == "budget_exhausted"
    assert result.harness_state["stage"] != "complete"
    assert bool(result.review_evidence) == (budget >= 2)
    assert bool(result.reader_observations) == (budget >= 2)


class PagingReader(RecordingReader):
    async def ainvoke(self, request):
        self.requests.append(request)
        text = "x" * request.max_chars
        return ReferenceReadResult(namespace=request.namespace, read_id=f"read_{request.char_start}",
            work_id="work-1", artifact_id=request.artifact_id, role="extracted_text",
            char_start=request.char_start, char_end=request.char_start + len(text), text=text,
            has_more=True, sha256=hashlib.sha256(b"whole artifact").hexdigest())


@pytest.mark.parametrize("limits,expected", [({"max_tool_calls": 1}, [8000]),
    ({"max_total_read_chars": 9000}, [8000, 1000])])
def test_tool_and_character_budgets_allow_reassessment(limits, expected):
    reader = PagingReader()
    result = asyncio.run(reviewer(ChoosingClient(), reader, **limits).review_card(_request()))
    assert result.status.value == "reviewed", result
    assert [r.max_chars for r in reader.requests] == expected
    assert not any(r["artifact_read_complete"] for r in result.harness_state["read_history"])
    assert result.harness_state["support_history"][-1]["stage"] == "reassess_card"


def test_stale_cross_invocation_choices_and_direct_tools_are_rejected():
    req, registry = _request(), ResearcherToolRegistry([ReviewerReaderTool(RecordingReader())])
    def make():
        return ReviewerReadingPolicy(registry, req, catalog=[], max_read_chars=20, max_tool_calls=1)
    first, second = make(), make()
    option = next(iter(first.choices()))
    call = first.selection({"option_id": option}, "c1")
    with pytest.raises(ValueError):
        second.resolve(call)
    first.resolve(call)
    with pytest.raises(ValueError):
        first.resolve(call)
    with pytest.raises(ValueError):
        first.resolve(ModelToolCall(id="c2", name="reader", arguments={"artifact_id": "artifact-1"}))
    assert first.remaining_tools == 1 and second.remaining_tools == 1


def test_early_finish_gets_bounded_correction_without_reading():
    async def inject(client, context):
        if len(client.calls) == 1:
            return ModelResponse(content='{"novelty_point_id":"NP-1","support_reason":"pretend finished"}')
    client = ChoosingClient(read=False, inject=inject)
    result = asyncio.run(reviewer(client).review_card(_request()))
    assert result.status.value == "reviewed" and len(client.calls) == 4
    assert not result.reader_observations


@pytest.mark.parametrize("field,value", [("verdict", "partially_novel"), ("read_citations", []),
    ("review_evidence", []), ("support_status", "usable"), ("confidence", 0.8)])
def test_model_cannot_supply_execution_or_point_fields(field, value):
    with pytest.raises(ValueError):
        ReviewerSupportDraft(novelty_point_id="NP-1", support_reason="test", **{field: value})


def test_fabricated_evidence_fails_without_losing_actual_reads():
    client = ChoosingClient(draft={"highly_relevant_works": [{"work_id": "work-1", "card_ids": ["C-1"],
        "evidence_ids": ["fake"], "relevance_reason": "invented"}]})
    result = asyncio.run(reviewer(client).review_card(_request()))
    assert result.incomplete_reason == "technical_error"
    assert len(result.review_evidence) == len(result.reader_observations) == 1
    assert result.harness_state["stage"] == "draft"


def test_reader_failure_is_charged_then_reassessed():
    class Broken(RecordingReader):
        async def ainvoke(self, request):
            self.requests.append(request)
            raise FileNotFoundError("test missing source")
    source = Broken()
    result = asyncio.run(reviewer(ChoosingClient(), source, max_tool_calls=1).review_card(_request()))
    assert result.status.value == "reviewed", result
    assert result.execution_issues and len(source.requests) == 1
    assert not result.review_evidence and not result.reader_observations
    assert result.harness_state["remaining_tool_calls"] == 0
    assert result.harness_state["support_history"][-1]["stage"] == "reassess_card"


def test_timeout_after_read_preserves_ledger():
    async def inject(client, context):
        if context["state"]["stage"] == "reassess_card":
            await asyncio.sleep(1)
    result = asyncio.run(reviewer(ChoosingClient(inject=inject), card_timeout_seconds=0.1).review_card(_request()))
    assert result.incomplete_reason == "budget_exhausted"
    assert len(result.review_evidence) == len(result.reader_observations) == 1


def test_concurrent_cards_have_separate_menus_ledgers_and_registry_corrections():
    async def run():
        client = ChoosingClient()
        service = reviewer(client, max_tool_calls=1)
        first, second = _request(), _request()
        second.cards[0].card_id = "C-2"
        results = await asyncio.gather(service.review_card(first), service.review_card(second))
        return results, service
    results, service = asyncio.run(run())
    assert all(r.status.value == "reviewed" for r in results)
    assert [r.review_evidence[0].origin_card_id for r in results] == ["C-1", "C-2"]
    assert len({r.review_evidence[0].evidence_id for r in results}) == 2
    assert all(r.harness_state["remaining_tool_calls"] == 0 for r in results)
    assert not service.tools._pending_corrections


def test_concurrent_failure_does_not_become_another_cards_recovery():
    class FirstFails(RecordingReader):
        async def ainvoke(self, request):
            if not self.requests:
                self.requests.append(request)
                raise FileNotFoundError("first invocation only")
            return await super().ainvoke(request)

    async def run():
        service = reviewer(ChoosingClient(), FirstFails(), max_tool_calls=1)
        first, second = _request(), _request()
        second.cards[0].card_id = "C-2"
        return await asyncio.gather(service.review_card(first), service.review_card(second)), service

    results, service = asyncio.run(run())
    failed = next(r for r in results if r.execution_issues)
    successful = next(r for r in results if not r.execution_issues)
    assert not failed.review_evidence and len(successful.review_evidence) == 1
    assert all(r.status.value == "reviewed" for r in results)
    assert not service.tools._pending_corrections


def test_read_failure_can_continue_with_another_authorized_artifact():
    class OneMissing(RecordingReader):
        async def ainvoke(self, request):
            if request.artifact_id == "artifact-1":
                self.requests.append(request)
                raise FileNotFoundError("one material missing")
            return await super().ainvoke(request)

    request = _request()
    request.evidence.append(request.evidence[0].model_copy(update={"evidence_id": "E-2", "artifact_id": "artifact-2"}))
    request.cards[0].evidence_ids.append("E-2")
    reader = OneMissing()
    result = asyncio.run(reviewer(ChoosingClient(), reader, max_tool_calls=2).review_card(request))
    assert result.status.value == "reviewed"
    assert [r.artifact_id for r in reader.requests] == ["artifact-1", "artifact-2"]
    assert len(result.execution_issues) == 1 and len(result.review_evidence) == 1
    assert result.review_evidence[0].artifact_id == "artifact-2"
    assert result.harness_state["support_history"][-1]["stage"] == "reassess_card"


def test_empty_eof_is_a_read_observation_but_not_evidence():
    class Empty(RecordingReader):
        async def ainvoke(self, request):
            original = await super().ainvoke(request)
            return original.model_copy(update={"text": "", "char_end": request.char_start})
    result = asyncio.run(reviewer(ChoosingClient(), Empty()).review_card(_request()))
    assert result.status.value == "reviewed"
    assert len(result.reader_observations) == 1 and not result.review_evidence


@pytest.mark.parametrize("change", [{"work_id": "another"}, {"namespace": "subject_reference"},
    {"char_start": 1}, {"char_end": 100}, {"text": "invented"}, {"sha256": "bad"}])
def test_ledger_rejects_wrong_scope_range_hash_or_content(change):
    req = _request()
    tool = ReviewerReaderTool(RecordingReader())
    catalog = tool.material_catalog(req)
    catalog[0]["content_hash"] = hashlib.sha256(b"source text").hexdigest()
    ledger = ReviewerEvidenceLedger(req, catalog)
    call = ModelToolCall(id="c", name="reader", arguments={"artifact_id": "artifact-1", "char_start": 0, "max_chars": 20})
    raw = asyncio.run(tool.ainvoke(tool.args_schema.model_validate(call.arguments), scope=req)).payload["read_result"]
    with pytest.raises(ValueError):
        ledger.register({**raw, **change}, call)
    assert not ledger.evidence and not ledger.audits


def test_support_prompt_loads_and_legacy_config_remains_available():
    from pathlib import Path
    prompts = PromptLibrary(Path("backend/src/novelty_agent_framework/prompts"))
    assert "不作点级" in prompts.render("reviewer/review_support").system
    service = reviewer(ChoosingClient())
    assert service.config.controlled_reading
    assert not replace(service.config, controlled_reading=False).controlled_reading


@pytest.mark.parametrize("max_steps,completed,calls", [(3, False, 3), (4, True, 4)])
def test_format_repair_shares_card_call_budget(max_steps, completed, calls):
    class RepairClient(ChoosingClient):
        async def acomplete(self, messages, *, options=None):
            if len(self.calls) == 3:
                self.calls.append((messages, options, {}))
                assert options.tools == () and options.tool_choice == "none"
                return ModelResponse(content=json.dumps({"novelty_point_id": "NP-1", "support_reason": "修复后的说明"}))
            result = await super().acomplete(messages, options=options)
            return ModelResponse(content="invalid") if len(self.calls) == 3 else result
    client = RepairClient(read=False)
    result = asyncio.run(reviewer(client, max_steps=max_steps).review_card(_request()))
    assert (result.status.value == "reviewed") == completed
    assert len(client.calls) == calls
    if not completed:
        assert result.incomplete_reason == "budget_exhausted"


def test_repair_transport_timeout_is_not_card_budget_exhaustion():
    class RepairTimeout(ChoosingClient):
        async def acomplete(self, messages, *, options=None):
            if len(self.calls) == 3:
                raise TimeoutError("transport timeout before card deadline")
            result = await super().acomplete(messages, options=options)
            return ModelResponse(content="invalid") if len(self.calls) == 3 else result
    result = asyncio.run(reviewer(RepairTimeout(read=False)).review_card(_request()))
    assert result.incomplete_reason == "technical_error"
    assert result.execution_issues[-1].code == "model.timeout"


def test_new_support_checkpoint_recovers_summary_without_rereading(tmp_path):
    from test_reviewer_failure_classification import local_tool
    from test_reviewer_summary_checkpoint import Client, output
    request, tool, _ = local_tool(tmp_path)
    service = NoveltyEvidenceReviewer(ChoosingClient(), config=EvidenceReviewerConfig(max_tool_calls=1),
                                      tool_registry=ResearcherToolRegistry([tool]))
    support = asyncio.run(service.review_card(request))
    assert support.status.value == "reviewed", support
    assert support.review_evidence
    rows = [row(support, request)]
    summary = json.loads(output(request).content)
    summary["highly_relevant_works"][0]["evidence_ids"] = [support.review_evidence[0].evidence_id]
    client = Client(TimeoutError("injected"), ModelResponse(content=json.dumps(summary)))
    service.model_client = client
    first = asyncio.run(service.summarize_with_checkpoint(request, rows, output_root=tmp_path, run_id="new-support"))
    assert first.durable and first.status == "failed"
    saved = next(tmp_path.rglob("checkpoint.json")).read_bytes()
    second = asyncio.run(service.recover_summary(request, output_root=tmp_path, run_id="new-support",
                                                checkpoint_id=first.checkpoint_id))
    assert second.status == "completed" and len(client.calls) == 2
    assert next(tmp_path.rglob("checkpoint.json")).read_bytes() == saved
    assert second.partial_card_results == rows
    assert second.review.review_evidence == support.review_evidence
    assert all(call[1].tools == () for call in client.calls)


def test_new_support_partial_result_passes_report_integrity(tmp_path):
    from test_report_partial_integrity import case
    from novelty_agent_framework.core.integrity_gates import validate_report_integrity
    report, kwargs, _ = case(tmp_path)
    old = report.partial_card_reviews[0].review
    support = ReviewerSupportReview(novelty_point_id=old.novelty_point_id, status="reviewed",
        support_reason="有限支撑，尚无点级结论", support_status="partially_usable", harness_state={"stage": "complete"},
        highly_relevant_works=old.highly_relevant_works, feature_comparisons=old.feature_comparisons,
        review_evidence=old.review_evidence, reader_observations=old.reader_observations)
    report.partial_card_reviews[0].review = support
    result = validate_report_integrity(report, **kwargs)
    assert result.validation_passed, result.issues


def test_three_unknown_features_remain_without_single_card_verdict():
    request = _request()
    request.novelty_point.technical_features = ["a", "b", "c"]
    client = ChoosingClient(read=False, draft={"highly_relevant_works": [], "feature_comparisons": [
        {"feature_id": f"F{i}", "work_id": "work-1", "relation": "unknown", "basis_type": "insufficient",
         "reason": "缺少直接依据", "evidence_refs": []} for i in range(1, 4)]})
    result = asyncio.run(reviewer(client).review_card(request))
    assert result.status.value == "reviewed"
    assert all(item.relation == "unknown" and not item.evidence_refs for item in result.feature_comparisons)
    assert not {"verdict", "confidence"}.intersection(result.model_dump())
    # This deliberately says nothing about future point-level unknown adjudication.
