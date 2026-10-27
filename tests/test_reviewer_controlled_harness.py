"""Staged Card usability decisions, real local Reader, and summary integration."""
import asyncio
import json
from dataclasses import replace

import pytest

from backend.env import ModelResponse, ModelToolCall
from novelty_agent_framework.agents.evidence_reviewer import (
    EvidenceReviewerConfig, NoveltyEvidenceReviewer, _compact_summary_rows,
)
from novelty_agent_framework.schemas.domain import ReviewerSupportDraft, ReviewerSupportReview
from novelty_agent_framework.schemas.failures import CodedFailure, FailureCode
from novelty_agent_framework.tools import ResearcherToolRegistry
from novelty_agent_framework.workflows.reviewer_reading_policy import ReviewerReadingPolicy
from test_novelty_point_reviewer import _request
from test_reviewer_failure_classification import local_tool
from test_reviewer_summary_checkpoint import setup_case, output


def select(policy, label):
    key = next(key for key, value in policy.choices().items() if value.startswith(label))
    return policy.resolve(policy.selection({"option_id": key}, "selection"))


def policy_case():
    return ReviewerReadingPolicy(ResearcherToolRegistry(), _request(), catalog=[],
                                 max_read_chars=100, max_tool_calls=2)


@pytest.mark.parametrize("label,status", [
    ("可用", "usable"), ("部分可用", "partially_usable"),
    ("待核验", "needs_verification"), ("不可用", "unusable"),
])
def test_support_selection_never_chooses_reading(label, status):
    policy = policy_case()
    assert len(policy.choices()) == 4
    assert not any("继续阅读正文" in value for value in policy.choices().values())
    assert select(policy, label) is None
    assert policy.stage == "decide_reading" and policy.reading_decision == "not_selected"
    assert policy.support_history[-1]["status"] == status
    assert not policy.can_finish
    select(policy, "不继续")
    assert policy.can_finish and policy.reading_decision == "skip"
    assert policy.snapshot()["reading_effect"] is None


def test_foreign_stale_and_label_options_are_rejected_before_io():
    policy, other = policy_case(), policy_case()
    stale = next(iter(policy.choices()))
    foreign = next(iter(other.choices()))
    select(policy, "可用")
    for value, code in [(stale, FailureCode.HARNESS_OPTION_STALE),
                        (foreign, FailureCode.HARNESS_OPTION_UNKNOWN),
                        (next(iter(policy.choices().values())), FailureCode.HARNESS_OPTION_LABEL)]:
        with pytest.raises(CodedFailure) as exc:
            policy.resolve(policy.selection({"option_id": value}, "bad"))
        assert exc.value.code == code
    assert policy.stage == "decide_reading" and policy.reading_decision == "not_selected"


class ChoosingClient:
    def __init__(self, request, choices, *, inject_native=False, auto_continue=False):
        self.request, self.choices = request, list(choices)
        self.calls = []
        self.inject_native = inject_native
        self.auto_continue = auto_continue
        self.reads = []

    async def acomplete(self, messages, *, options=None):
        self.calls.append((messages, options))
        assert options.tools == () and options.tool_choice == "none"
        if self.inject_native:
            self.inject_native = False
            return ModelResponse(content=None, tool_calls=(ModelToolCall(
                id="forged", name="reader", arguments={"artifact_id": "outside"}),))
        for message in messages:
            try:
                value = json.loads((message.content or "").removeprefix("Harness observation:\n"))
            except (ValueError, TypeError):
                continue
            if isinstance(value, dict) and value.get("evidence") and "succeeded" in value:
                for evidence in value["evidence"]:
                    if evidence["evidence_id"] not in [r["evidence_id"] for r in self.reads]:
                        self.reads.append(evidence)
        menu_message = next((m.content for m in reversed(messages)
                             if (m.content or "").startswith("Current Harness choices")), None)
        menu = json.loads(menu_message.split("\n", 1)[1].split("\nReturn", 1)[0]) if menu_message else {}
        if menu:
            label = ("继续读取当前正文" if self.auto_continue and
                     any(value.startswith("继续读取当前正文") for value in menu.values())
                     else self.choices.pop(0))
            key = next(key for key, value in menu.items() if value.startswith(label))
            assert options.response_format["json_schema"]["schema"]["properties"]["option_id"]["enum"] == list(menu)
            return ModelResponse(content=json.dumps({"option_id": key}))
        draft = {"novelty_point_id": self.request.novelty_point.point_id,
                 "support_reason": "Test: initial usability was provisional; preserve the observed reading limits.",
                 "highly_relevant_works": [{"work_id": self.request.evidence[0].work_id,
                    "card_ids": [self.request.cards[0].card_id],
                    "evidence_ids": [self.request.evidence[0].evidence_id] +
                                    [r["evidence_id"] for r in self.reads], "relevance_reason": "Visible evidence"}]}
        return ModelResponse(content=json.dumps(draft))


def controlled(client, tool=None, **kwargs):
    return NoveltyEvidenceReviewer(client,
        tool_registry=ResearcherToolRegistry([tool] if tool else []),
        config=EvidenceReviewerConfig(guided_reading=True, max_steps=14, **kwargs))


def row(request, review):
    return {"index": 1, "card_id": request.cards[0].card_id,
            "novelty_point_id": request.novelty_point.point_id,
            "status": "completed", "review": review.model_dump(mode="json")}


def test_initial_usable_can_skip_without_reading_and_cannot_supply_verdict():
    request = _request()
    client = ChoosingClient(request, ["可用", "不继续"])
    review = asyncio.run(controlled(client).review_card(request))
    assert isinstance(review, ReviewerSupportReview)
    assert review.status.value == "reviewed" and review.support_status.value == "usable"
    assert review.reader_observations == [] and len(client.calls) == 3
    assert "verdict" not in review.model_dump()
    for field in ("verdict", "support_status", "harness_state", "read_citations", "read_id", "char_start"):
        with pytest.raises(ValueError):
            ReviewerSupportDraft.model_validate({"novelty_point_id": "NP-1", "support_reason": "reason", field: "forged"})


def test_usable_can_read_and_revise_initial_state_even_at_tool_budget(tmp_path):
    request, tool, _ = local_tool(tmp_path)
    client = ChoosingClient(request, ["可用", "继续阅读正文", "不可用", "回读有帮助"], inject_native=True)
    review = asyncio.run(controlled(client, tool, max_tool_calls=1, max_total_read_chars=300).review_card(request))
    assert review.status.value == "reviewed", review.model_dump()
    assert review.support_status.value == "unusable"
    state = review.harness_state
    assert [item["status"] for item in state["support_history"]] == ["usable", "unusable"]
    assert state["reading_decision"] == "read_fulltext" and state["reading_effect"] == "helpful"
    assert not state["artifact_read_complete"]  # Only 300 characters, never call this full-paper coverage.
    assert len(review.reader_observations) == 1
    assert len(review.review_evidence) == 1
    assert len(review.review_evidence[0].exact_quote) == 300
    compact, ids = _compact_summary_rows(request, [row(request, review)])
    assert review.review_evidence[0].evidence_id in ids
    assert compact[0]["review"]["harness_state"]["reading_effect"] == "helpful"


def test_decision_budget_does_not_invent_missing_support_or_reading_selection():
    request = _request()
    client = ChoosingClient(request, ["待核验"])
    reviewer = controlled(client)
    reviewer.config = replace(reviewer.config, max_steps=2)
    reviewer.harness.config = replace(reviewer.harness.config, max_turns=2)
    result = asyncio.run(reviewer.review_card(request))
    assert result.status.value == "insufficient_evidence"
    assert result.incomplete_reason == "budget_exhausted"
    assert result.support_status.value == "needs_verification"
    assert result.harness_state["reading_decision"] == "not_selected"
    assert len(client.calls) == 2


def test_summary_consumes_new_results_and_preserves_their_read_evidence(tmp_path):
    request, tool, _ = local_tool(tmp_path)
    client = ChoosingClient(request, ["待核验", "继续阅读正文", "部分可用", "回读有帮助"])
    reviewer = controlled(client, tool, max_tool_calls=1, max_total_read_chars=300)
    card = asyncio.run(reviewer.review_card(request))
    assert card.review_evidence
    from test_reviewer_summary_checkpoint import Client
    summary_client = Client(output(request, reviewed=True))
    reviewer.model_client = summary_client
    result = asyncio.run(reviewer.summarize_with_checkpoint(request, [row(request, card)],
                         output_root=tmp_path, run_id="controlled-summary"))
    assert result.status == "completed" and result.durable
    assert result.review.verdict.value == "partially_novel"
    assert result.review.review_evidence == card.review_evidence
    sent = json.loads(summary_client.calls[0][0][1].content)
    assert sent["card_reviews"][0]["review"]["support_status"] == "partially_usable"
    assert "verdict" not in sent["card_reviews"][0]["review"]


def test_transport_recovery_uses_same_checkpoint_without_rereading(tmp_path):
    request, reviewer, client, rows, _ = setup_case(tmp_path)
    reviewer.config = replace(reviewer.config, recover_summary_on_failure=True)
    before = len(client.calls)
    result = asyncio.run(reviewer.summarize_with_checkpoint(request, rows, output_root=tmp_path, run_id="auto"))
    assert result.status == "completed" and result.attempt_id == "recovery-1"
    assert len(client.calls) == before + 2
    assert list(tmp_path.rglob("initial/result.json")) and list(tmp_path.rglob("recovery-1/result.json"))
    assert all(options.tools == () for _, options in client.calls[before:])


def test_semantic_insufficiency_is_not_retried(tmp_path):
    request, reviewer, client, rows, _ = setup_case(tmp_path)
    reviewer.config = replace(reviewer.config, recover_summary_on_failure=True)
    client.responses = [output(request)]
    before = len(client.calls)
    result = asyncio.run(reviewer.summarize_with_checkpoint(request, rows, output_root=tmp_path, run_id="semantic"))
    assert result.status == "completed" and result.attempt_id == "initial"
    assert len(client.calls) == before + 1


@pytest.mark.parametrize("legacy", [True, False])
def test_application_factories_enable_the_controlled_protocol(legacy):
    from novelty_agent_framework.config import load_application_config, build_workflow
    from novelty_agent_framework.config.loader import legacy_shape
    from novelty_agent_framework.config.experiment import configured_prompt_names
    config = load_application_config(environ={})
    reviewer = build_workflow(legacy_shape(config) if legacy else config).services.reviewer
    assert reviewer.config.guided_reading and reviewer.config.recover_summary_on_failure
    assert configured_prompt_names(config)["reviewer"]["review"] == config.reviewer.support_prompt


def test_full_body_reading_reaches_eof_without_repeating_ranges(tmp_path):
    request, tool, _ = local_tool(tmp_path)
    client = ChoosingClient(request, ["部分可用", "继续阅读正文", "可用", "回读有帮助"], auto_continue=True)
    reviewer = controlled(client, tool, max_tool_calls=30, max_total_read_chars=240_000)
    reviewer.config = replace(reviewer.config, max_steps=40)
    reviewer.harness.config = replace(reviewer.harness.config, max_turns=40)
    result = asyncio.run(reviewer.review_card(request))
    assert result.status.value == "reviewed", result.model_dump()
    assert result.harness_state["artifact_read_complete"]
    audits = result.reader_observations
    assert audits and audits[0].char_start == 0
    assert all(a.char_end == b.char_start for a, b in zip(audits, audits[1:]))
    entry = next(e for e in tool.material_catalog(request) if e["artifact_id"] == audits[0].artifact_id)
    assert audits[-1].char_end == entry["readable_chars"]


@pytest.mark.parametrize("summary_failed", [False, True])
def test_workflow_gate_and_report_accept_new_single_card_results(tmp_path, summary_failed):
    from novelty_agent_framework.schemas import PaperInput, NoveltyBrief, ReportNarrativeDraft
    from novelty_agent_framework.workflows import NoveltyWorkflow
    from novelty_agent_framework.core.integrity_gates import validate_report_integrity
    from novelty_agent_framework.persistence import persist_workflow_input
    from novelty_agent_framework.core.report_binding import assemble_report_from_draft
    from test_workflow_reviewer import _workflow
    request, tool, _ = local_tool(tmp_path)

    class WorkflowClient(ChoosingClient):
        async def acomplete(self, messages, *, options=None):
            try:
                value = json.loads(messages[1].content)
            except (ValueError, TypeError):
                value = {}
            if "card_reviews" in value:
                self.calls.append((messages, options))
                if summary_failed:
                    raise TimeoutError("injected summary failure")
                return output(request, reviewed=True)
            return await super().acomplete(messages, options=options)

    client = WorkflowClient(request, ["可用", "继续阅读正文", "部分可用", "回读有帮助"])
    reviewer = controlled(client, tool, max_tool_calls=1, max_total_read_chars=300,
                          recover_summary_on_failure=True)
    class NarrativeCoordinator:
        def synthesize(self, paper, *, brief, evidence, novelty_reviews, **kwargs):
            return assemble_report_from_draft(ReportNarrativeDraft(conclusions=[{
                "novelty_point_id": request.novelty_point.point_id, "summary": "Test narrative"}]),
                paper_id=paper.paper_id, novelty_reviews=novelty_reviews,
                novelty_points=brief.novelty_points, evidence_cards=evidence)

    services = replace(_workflow(reviewer=reviewer).services, coordinator=NarrativeCoordinator())
    workflow = NoveltyWorkflow(services, output_root=tmp_path)
    workflow.reference_store = tool.reader.reference_store
    state = {"paper": PaperInput(paper_id=request.subject_paper_id, title="Test", full_text="Subject"),
             "brief": NoveltyBrief(paper_summary="Test", novelty_points=[request.novelty_point],
                                   research_tasks=request.tasks),
             "run_id": "controlled-workflow", "novelty_points": [request.novelty_point],
             "all_research_tasks": request.tasks, "raw_evidence": request.evidence,
             "raw_evidence_cards": request.cards, "validator_accepted_cards": request.cards}
    persist_workflow_input(state["paper"], output_root=tmp_path, run_id=state["run_id"])
    async def run():
        reviewed = await workflow._review_evidence(state)
        assert reviewed["evidence_cards"] == request.cards
        assert reviewed["review_card_results"][0]["review"]["kind"] == "card_support"
        state.update(reviewed)
        state.update(await workflow._validate_synthesis_input(state))
        synthesized = await workflow._synthesize_report(state)
        return synthesized["report"]
    report = asyncio.run(run())
    if summary_failed:
        assert len(report.partial_card_reviews) == 1
        assert isinstance(report.partial_card_reviews[0].review, ReviewerSupportReview)
        assert report.conclusions[0].verdict is None
    else:
        assert not report.partial_card_reviews
        assert report.conclusions[0].verdict.value == "partially_novel"
    gate = validate_report_integrity(report, novelty_points=[request.novelty_point],
        evidence_cards=request.cards, novelty_reviews=state["novelty_reviews"],
        evidence=request.evidence, reference_store=workflow.reference_store)
    assert gate.validation_passed, gate
