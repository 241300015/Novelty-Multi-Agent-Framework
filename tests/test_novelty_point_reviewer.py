"""查新点级 Reviewer、Reader scope 与输出引用契约。"""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pytest
from novelty_agent_framework.agents.evidence_reviewer import EvidenceReviewerConfig
from pydantic import ValidationError
from backend.env import ModelResponse, ModelToolCall, PromptLibrary
from novelty_agent_framework.agents import NoveltyEvidenceReviewer
from novelty_agent_framework.agents import (
    DemoCoordinator,
    DemoSearchPlanner,
    DemoTaskResearcher,
)
from novelty_agent_framework.schemas import (
    ArtifactRole,
    ArtifactNamespace,
    Evidence,
    EvidenceCard,
    NoveltyPoint,
    NoveltyPointReviewRequest,
    NoveltyPointReview,
    PaperInput,
    ReferenceReadResult,
    ResearchTask,
)
from novelty_agent_framework.tools import ReviewerReaderTool, ResearcherToolRegistry
from novelty_agent_framework.workflows import (
    NoveltyWorkflow,
    NoveltyWorkflowServices,
)


class ScriptedClient:
    def __init__(self, *responses: ModelResponse):
        self.responses = list(responses)
        self.calls = []

    async def acomplete(self, messages, *, options=None):
        self.calls.append((list(messages), options))
        return self.responses.pop(0)


class RecordingReader:
    max_chars_per_read = 16_000

    def __init__(self):
        self.requests = []

    async def ainvoke(self, request):
        self.requests.append(request)
        return ReferenceReadResult(
            namespace=request.namespace,
            read_id="read-1",
            work_id="work-1",
            artifact_id=request.artifact_id,
            role=ArtifactRole.EXTRACTED_TEXT,
            char_start=request.char_start,
            char_end=request.char_start + len("source text"),
            text="source text",
            has_more=False,
            sha256=hashlib.sha256(b"source text").hexdigest(),
        )


def _request() -> NoveltyPointReviewRequest:
    point = NoveltyPoint(point_id="NP-1", claim="组合技术特征")
    task = ResearchTask(
        task_id="T-1",
        novelty_point_id="NP-1",
        task_type="prior-art",
        language="zh",
    )
    evidence = Evidence(
        evidence_id="E-1",
        work_id="work-1",
        artifact_id="artifact-1",
        novelty_point_id="NP-1",
        task_id="T-1",
        quote="source text",
        interpretation="与查新点部分重合",
        confidence=0.9,
    )
    card = EvidenceCard(
        card_id="C-1",
        task_id="T-1",
        novelty_point_id="NP-1",
        document_title="Prior Work",
        main_contribution="prior contribution",
        relevance=0.9,
        confidence=0.9,
        evidence_ids=["E-1"],
    )
    return NoveltyPointReviewRequest(
        subject_paper_id="paper-1",
        novelty_point=point,
        tasks=[task],
        cards=[card],
        evidence=[evidence],
    )


def _review_json(status="reviewed") -> str:
    if status == "insufficient_evidence":
        return json.dumps(
            {
                "novelty_point_id": "NP-1",
                "status": status,
                "supplement_request": {
                    "reason": "缺少实现细节",
                    "missing_aspects": ["实现细节"],
                },
            }
        )
    return json.dumps(
        {
            "novelty_point_id": "NP-1",
            "status": status,
            "verdict": "partially_novel",
            "verdict_reason": "既有工作覆盖部分特征，但未覆盖完整组合。",
            "confidence": 0.84,
            "highly_relevant_works": [
                {
                    "work_id": "work-1",
                    "card_ids": ["C-1"],
                    "evidence_ids": ["E-1"],
                    "relevance_reason": "直接覆盖一个核心特征。",
                }
            ],
        },
        ensure_ascii=False,
    )


def _reviewer(client, reader):
    return NoveltyEvidenceReviewer(
        client,
        tool_registry=ResearcherToolRegistry([ReviewerReaderTool(reader)]),
        config=EvidenceReviewerConfig(controlled_reading=False),
    )


def test_batch_reviewer_reader_checks_each_artifact_scope():
    reader = RecordingReader()
    tool = ReviewerReaderTool(reader)
    observation = asyncio.run(tool.ainvoke(tool.args_schema.model_validate({"reads": [
        {"artifact_id": "artifact-1"}, {"artifact_id": "outside"},
    ]}), scope=_request()))
    assert [request.artifact_id for request in reader.requests] == ["artifact-1"]
    assert len(observation.payload["read_results"]) == 1
    assert observation.payload["read_errors"][0]["error_type"] == "PermissionError"


def test_review_request_rejects_unresolved_card_evidence_ids():
    request = _request()
    with pytest.raises(ValidationError, match="absent from request evidence"):
        NoveltyPointReviewRequest(
            subject_paper_id=request.subject_paper_id,
            novelty_point=request.novelty_point,
            tasks=request.tasks,
            cards=request.cards,
            evidence=[],
        )


def test_point_review_can_finish_with_traceable_relevant_work():
    result = asyncio.run(
        _reviewer(ScriptedClient(ModelResponse(content=_review_json())), RecordingReader())
        .review(_request())
    )
    assert result.status.value == "reviewed"
    assert result.verdict.value == "partially_novel"
    assert result.highly_relevant_works[0].evidence_ids == ["E-1"]


def test_reviewer_reader_call_is_returned_to_model():
    reader = RecordingReader()
    client = ScriptedClient(
        ModelResponse(
            content=None,
            tool_calls=(
                ModelToolCall(
                    id="call-1",
                    name="reader",
                    arguments={"artifact_id": "artifact-1", "max_chars": 100},
                ),
            ),
        ),
        ModelResponse(content=_review_json()),
    )
    result = asyncio.run(_reviewer(client, reader).review(_request()))
    assert result.status.value == "reviewed"
    assert reader.requests[0].artifact_id == "artifact-1"
    observation = json.loads(client.calls[1][0][-1].content)
    assert observation["succeeded"] is True
    assert observation["read_result"]["text"] == "source text"


def test_reviewer_reader_rejects_artifact_outside_point_scope():
    reader = RecordingReader()
    client = ScriptedClient(
        ModelResponse(
            content=None,
            tool_calls=(
                ModelToolCall(
                    id="call-bad",
                    name="reader",
                    arguments={"artifact_id": "artifact-other"},
                ),
            ),
        ),
        ModelResponse(content=_review_json("insufficient_evidence")),
    )
    result = asyncio.run(_reviewer(client, reader).review(_request()))
    assert result.status.value == "insufficient_evidence"
    assert reader.requests == []
    observation = json.loads(client.calls[1][0][-1].content)
    assert observation["succeeded"] is False
    assert "outside reviewer scope" in observation["error"]


def test_insufficient_evidence_does_not_require_a_verdict():
    result = asyncio.run(
        _reviewer(
            ScriptedClient(ModelResponse(content=_review_json("insufficient_evidence"))),
            RecordingReader(),
        ).review(_request())
    )
    assert result.verdict is None
    assert result.confidence is None
    assert result.supplement_request is not None


def test_review_card_and_summary_receive_evidence_boundary_templates():
    root = Path(__file__).resolve().parents[1] / "backend/src/novelty_agent_framework/prompts"
    client = ScriptedClient(ModelResponse(content=_review_json()),
                            ModelResponse(content=_review_json("insufficient_evidence")))
    reviewer = NoveltyEvidenceReviewer(
        client, prompts=PromptLibrary(root),
        tool_registry=ResearcherToolRegistry([ReviewerReaderTool(RecordingReader())]),
        config=EvidenceReviewerConfig(controlled_reading=False),
    )
    card = asyncio.run(reviewer.review_card(_request()))
    summary = asyncio.run(reviewer.summarize_reviews(_request(), [_indexed_review()]))
    assert card.status.value == "reviewed"
    assert summary.status.value == "insufficient_evidence"
    card_system = client.calls[0][0][0].content
    summary_system = client.calls[1][0][0].content
    for system in (card_system, summary_system):
        assert "摘要、局部片段或截短引文未提及" in system
        assert "status=insufficient_evidence" in system
    assert "本轮只核验一张 Card" in card_system
    assert "quote_truncated=true" in summary_system
    assert "review_schema" in client.calls[1][0][1].content
    assert client.calls[1][1].tool_choice == "none"


@pytest.mark.parametrize("card_only", [True, False])
def test_reviewer_does_not_lexically_override_absence_claim(card_only):
    output = json.loads(_review_json())
    output["verdict_reason"] = "该摘要未使用目标算法，因此查新点部分新颖。"
    output["highly_relevant_works"][0]["relevance_reason"] = "文献未采用目标算法。"
    client = ScriptedClient(ModelResponse(content=json.dumps(output, ensure_ascii=False)))
    reviewer = _reviewer(client, RecordingReader())
    request = _request()
    review = asyncio.run(reviewer.review_card(request) if card_only
                         else reviewer.summarize_reviews(request, [_indexed_review()]))
    assert review.status.value == "reviewed"
    assert review.verdict is not None
    assert review.highly_relevant_works[0].evidence_ids == ["E-1"]
    assert "未采用" in review.highly_relevant_works[0].relevance_reason


def test_summary_does_not_lexically_override_novel_verdict():
    output = json.loads(_review_json())
    output["verdict"] = "novel"
    output["verdict_reason"] = "摘要未披露目标机制，未覆盖完整组合，因此具有新颖性。"
    output["highly_relevant_works"][0]["relevance_reason"] = "文献未披露目标机制。"
    review = asyncio.run(_reviewer(
        ScriptedClient(ModelResponse(content=json.dumps(output, ensure_ascii=False))),
        RecordingReader(),
    ).summarize_reviews(_request(), [_indexed_review()]))
    assert review.status.value == "reviewed"
    assert review.verdict.value == "novel"
    assert "未披露" in review.highly_relevant_works[0].relevance_reason


def test_reviewer_keeps_limited_judgment_when_quote_explicitly_denies_feature():
    request = _request()
    request.evidence[0].quote = "We do not use the target algorithm."
    output = json.loads(_review_json())
    output["verdict_reason"] = "文献未使用目标算法，且原文明确说明这一点。"
    review = asyncio.run(_reviewer(
        ScriptedClient(ModelResponse(content=json.dumps(output, ensure_ascii=False))),
        RecordingReader(),
    ).review_card(request))
    assert review.status.value == "reviewed"


class PointReviewer:
    def __init__(self):
        self.requests = []

    async def review(self, request):
        self.requests.append(request)
        return NoveltyPointReview.model_validate_json(_review_json())


def _workflow(reviewer):
    return NoveltyWorkflow(
        NoveltyWorkflowServices(
            coordinator=DemoCoordinator(),
            task_researcher=DemoTaskResearcher(),
            search_planner=DemoSearchPlanner(),
            reviewer=reviewer,
        )
    )


def test_workflow_persists_point_reviews_without_filtering_or_routing(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    request = _request()
    reviewer = PointReviewer()
    workflow = _workflow(reviewer)
    state = {
        "paper": PaperInput(paper_id="paper-1", title="Paper", full_text="body"),
        "novelty_points": [request.novelty_point],
        "all_research_tasks": request.tasks,
        "raw_evidence": request.evidence,
        "raw_evidence_cards": request.cards,
        "validator_accepted_cards": request.cards,
        "evidence_cards": request.cards,
        "rejected_evidence": [],
    }
    reviewed = asyncio.run(workflow._review_evidence(state))
    assert reviewed["evidence_cards"] == request.cards
    assert reviewer.requests == [
        NoveltyPointReviewRequest(
            subject_paper_id="paper-1",
            novelty_point=request.novelty_point,
            tasks=request.tasks,
            cards=request.cards,
            evidence=request.evidence,
        )
    ]
    payload = json.loads(
        (tmp_path / "outputs/paper-1/novelty-reviews.json").read_text(encoding="utf-8")
    )
    assert payload["reviews"][0]["novelty_point_id"] == "NP-1"
    assert asyncio.run(
        workflow._route_after_evidence_sufficiency_check(
            {**state, **reviewed, "insufficient_final_evidence_points": []}
        )
    ) == "synthesize"


@pytest.mark.parametrize("failure", [None, "type_error", "wrong_point"])
@pytest.mark.parametrize("concurrency", [1, 2])
def test_parallel_reviews_preserve_point_bundles_order_and_failure_isolation(
    tmp_path, monkeypatch, failure, concurrency
):
    monkeypatch.chdir(tmp_path)
    seed = _request()
    points, tasks, evidence, cards = [], [], [], []
    for index in range(3):
        point_id = f"NP-{index + 1}"
        points.append(seed.novelty_point.model_copy(update={"point_id": point_id}))
        tasks.append(seed.tasks[0].model_copy(update={"novelty_point_id": point_id}))
        for document in range(2):
            key = f"{index}-{document}"
            evidence.append(seed.evidence[0].model_copy(update={
                "novelty_point_id": point_id, "evidence_id": f"E-{key}",
                "work_id": f"W-{key}", "artifact_id": f"A-{key}",
            }))
            cards.append(seed.cards[0].model_copy(update={
                "novelty_point_id": point_id, "card_id": f"C-{key}",
                "evidence_ids": [f"E-{key}"],
            }))

    async def run():
        class ConcurrentReviewer:
            active = 0
            peak = 0

            def __init__(self):
                self.overlap = asyncio.Event()
                self.requests = []

            async def review(self, request):
                self.requests.append(request)
                self.active += 1
                self.peak = max(self.peak, self.active)
                if self.active == concurrency:
                    self.overlap.set()
                try:
                    # With concurrency=2, serial scheduling fails the timeout.
                    await self.overlap.wait()
                    await asyncio.sleep(0)
                    point_id = request.novelty_point.point_id
                    if point_id == "NP-2" and failure == "type_error":
                        raise TypeError("internal reviewer failure")
                    return NoveltyPointReview.model_validate({
                        "novelty_point_id": "wrong" if point_id == "NP-2" and failure == "wrong_point" else point_id,
                        "status": "insufficient_evidence",
                        "supplement_request": {"reason": f"reviewed {point_id}"},
                    })
                finally:
                    self.active -= 1

        reviewer = ConcurrentReviewer()
        workflow = _workflow(reviewer)
        workflow.config = replace(workflow.config, max_concurrency=concurrency)
        result = await asyncio.wait_for(workflow._review_evidence({
            "paper": PaperInput(paper_id="paper-1", title="Paper", full_text="body"),
            "novelty_points": points, "all_research_tasks": tasks,
            "raw_evidence": evidence, "raw_evidence_cards": cards,
            "validator_accepted_cards": cards,
        }), timeout=5)
        assert reviewer.peak == concurrency
        assert len(reviewer.requests) == 3
        for request in reviewer.requests:
            point_id = request.novelty_point.point_id
            assert len(request.cards) == len(request.evidence) == 2
            assert all(c.novelty_point_id == point_id for c in request.cards)
            assert all(e.novelty_point_id == point_id for e in request.evidence)
            assert all(t.novelty_point_id == point_id for t in request.tasks)
        assert result["evidence_cards"] == cards
        assert [r.novelty_point_id for r in result["novelty_reviews"]] == [p.point_id for p in points]
        assert len(result["issues"]) == (1 if failure else 0)
        assert result["novelty_reviews"][0].supplement_request.reason == "reviewed NP-1"
        assert result["novelty_reviews"][2].supplement_request.reason == "reviewed NP-3"

    asyncio.run(run())


@pytest.mark.parametrize("failed_card", [False, True])
def test_card_parallel_checkpoint_then_single_summary(tmp_path, monkeypatch, failed_card):
    monkeypatch.chdir(tmp_path)
    request = _request()
    second = request.cards[0].model_copy(update={"card_id": "C-2"})
    cards = [*request.cards, second]

    async def run():
        class TwoStageReviewer:
            def __init__(self):
                self.started = 0
                self.barrier = asyncio.Event()
                self.summary_calls = 0

            async def review(self, request):
                raise AssertionError("unexpected point review")

            async def review_card(self, single):
                assert len(single.cards) == 1
                self.started += 1
                if self.started == 2:
                    self.barrier.set()
                await self.barrier.wait()
                if failed_card and single.cards[0].card_id == "C-2":
                    raise RuntimeError("single card failure")
                return NoveltyPointReview.model_validate_json(_review_json("insufficient_evidence"))

            async def summarize_reviews(self, full, rows):
                self.summary_calls += 1
                assert len(full.cards) == 2
                assert full.evidence == request.evidence
                saved = json.loads(Path("outputs/paper-1/novelty-reviews.json").read_text())
                assert saved["reviews"] == []
                assert saved["phase"] == "card_review"
                assert saved["card_reviews"] == rows
                assert [row["index"] for row in rows] == [1, 2]
                assert [row["status"] for row in rows] == ["completed", "failed" if failed_card else "completed"]
                return NoveltyPointReview.model_validate_json(_review_json())

        reviewer = TwoStageReviewer()
        result = await asyncio.wait_for(_workflow(reviewer)._review_evidence({
            "paper": PaperInput(paper_id="paper-1", title="Paper", full_text="body"),
            "novelty_points": [request.novelty_point], "all_research_tasks": request.tasks,
            "raw_evidence": request.evidence, "validator_accepted_cards": cards,
        }), 5)
        saved = json.loads(Path("outputs/paper-1/novelty-reviews.json").read_text())
        assert saved["phase"] == "complete"
        assert len(saved["card_reviews"]) == 2
        assert len(saved["reviews"]) == reviewer.summary_calls == 1
        assert result["evidence_cards"] == cards

    asyncio.run(run())


def _indexed_review():
    return {"index": 1, "card_id": "C-1", "novelty_point_id": "NP-1",
            "status": "completed", "review": json.loads(_review_json())}


def test_summary_is_one_tool_free_call_with_compact_verified_quotes():
    client = ScriptedClient(ModelResponse(content=_review_json()))
    reviewer = _reviewer(client, RecordingReader())
    request = _request()
    request.cards[0].main_contribution = "FULL_CARD_BODY_" * 1000
    request.tasks[0].description = "FULL_TASK_BODY_" * 1000
    request.evidence[0].interpretation = "FULL_INTERPRETATION_" * 1000
    request.evidence[0].provenance = {"detail": "FULL_PROVENANCE_" * 1000}
    result = asyncio.run(reviewer.summarize_reviews(request, [_indexed_review()]))
    assert result.status.value == "reviewed"
    assert len(client.calls) == 1
    messages, options = client.calls[0]
    assert options.tool_choice == "none" and options.tools == ()
    assert "source text" in messages[1].content
    payload = json.loads(messages[1].content)
    assert set(payload) == {"today", "novelty_point", "card_reviews", "review_schema"}
    assert payload["card_reviews"][0]["index"] == 1
    for marker in ("FULL_CARD_BODY_", "FULL_TASK_BODY_", "FULL_INTERPRETATION_", "FULL_PROVENANCE_"):
        assert marker not in messages[1].content
    assert request.cards[0].main_contribution.startswith("FULL_CARD_BODY_")


def test_summary_limits_quotes_and_excludes_unreviewed_evidence():
    request = _request()
    seed = request.evidence[0]
    request.evidence = [seed.model_copy(update={
        "evidence_id": f"E-{i}", "quote": str(i) * 900 + "UNSENT_SUFFIX",
    }) for i in range(1, 5)]
    request.cards[0].evidence_ids = [e.evidence_id for e in request.evidence]
    row = _indexed_review()
    row["review"]["highly_relevant_works"][0]["evidence_ids"] = ["E-1", "E-2", "E-3"]
    client = ScriptedClient(ModelResponse(content=_review_json()))
    asyncio.run(_reviewer(client, RecordingReader()).summarize_reviews(request, [row]))
    user = client.calls[0][0][1].content
    data = json.loads(user)["card_reviews"][0]
    assert len(data["key_quotes"]) == 3
    assert all(not q["quote_truncated"] for q in data["key_quotes"])
    assert data["omitted_quote_count"] == 0
    assert "UNSENT_SUFFIX" in user and "E-4" not in user


def test_summary_cannot_cite_evidence_not_verified_in_card_stage():
    request = _request()
    request.evidence.append(request.evidence[0].model_copy(update={"evidence_id": "E-unreviewed"}))
    request.cards[0].evidence_ids.append("E-unreviewed")
    output = json.loads(_review_json())
    output["highly_relevant_works"][0]["evidence_ids"] = ["E-unreviewed"]
    client = ScriptedClient(ModelResponse(content=json.dumps(output)))
    result = asyncio.run(_reviewer(client, RecordingReader()).summarize_reviews(request, [_indexed_review()]))
    assert result.status.value == "insufficient_evidence"
    assert "not verified" in result.supplement_request.reason


def test_summary_invalid_json_fails_closed_without_an_extra_model_call():
    client = ScriptedClient(ModelResponse(content="invalid JSON"))
    result = asyncio.run(_reviewer(client, RecordingReader()).summarize_reviews(_request(), []))
    assert result.status.value == "insufficient_evidence"
    assert len(client.calls) == 0  # No valid card results, so synthesis is skipped.


def test_summary_repairs_its_own_strict_draft_once():
    invalid = json.loads(_review_json())
    invalid["review_evidence"] = [{"evidence_id": "fabricated"}]
    client = ScriptedClient(ModelResponse(content=json.dumps(invalid)),
                            ModelResponse(content=_review_json()))
    result = asyncio.run(_reviewer(client, RecordingReader()).summarize_reviews(
        _request(), [_indexed_review()]))
    assert result.status.value == "reviewed"
    assert len(client.calls) == 2
    repair_schema = json.loads(client.calls[1][0][1].content)["schema"]
    assert "review_evidence" not in json.dumps(repair_schema)
    assert client.calls[1][1].tools == ()


def test_card_and_summary_timeouts_are_bounded():
    class WaitingClient:
        async def acomplete(self, messages, *, options=None):
            await asyncio.Event().wait()

    async def run():
        reviewer = _reviewer(WaitingClient(), RecordingReader())
        reviewer.config = replace(reviewer.config, card_timeout_seconds=0.01, summary_timeout_seconds=0.01)
        card = await asyncio.wait_for(reviewer.review_card(_request()), 1)
        summary = await asyncio.wait_for(reviewer.summarize_reviews(_request(), []), 1)
        assert card.status.value == summary.status.value == "insufficient_evidence"

    asyncio.run(run())
