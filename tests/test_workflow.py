"""Coordinator → TaskResearcher fan-out → Validator 主工作流测试。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import threading
from datetime import datetime, timezone
from pathlib import Path

import pytest

from novelty_agent_framework.agents import (
    DefaultEvidenceValidator,
    DemoCoordinator,
    DemoPointExtractor,
    DemoSearchPlanner,
    EvidenceValidationConfig,
    build_paper_digest,
)
from novelty_agent_framework.core.errors import WorkflowExecutionError
from novelty_agent_framework.core import RuntimeDebugConfig
from novelty_agent_framework.ports import ValidationResult
from novelty_agent_framework.persistence import ReferenceStore
from novelty_agent_framework.schemas import (
    Artifact,
    ArtifactRole,
    ContentExtent,
    Evidence,
    EvidenceCard,
    EvidenceLocator,
    EvidenceSource,
    NoveltyBrief,
    PaperInput,
    TaskResearchRequest,
    TaskResearchResult,
    TaskResearchStatus,
    Work,
    WorkType,
)
from novelty_agent_framework.workflows import (
    NoveltyWorkflow,
    NoveltyWorkflowConfig,
    NoveltyWorkflowServices,
)


@pytest.fixture(autouse=True)
def _isolated_outputs(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)


def make_paper(claims: int = 1) -> PaperInput:
    return PaperInput(
        paper_id="paper-test",
        title="证据驱动论文查新",
        abstract="测试摘要",
        full_text="测试正文",
        claimed_contributions=[f"创新声明 {index}" for index in range(1, claims + 1)],
    )


def make_card(
    request: TaskResearchRequest, evidence_id: str = "fixture-evidence"
) -> EvidenceCard:
    task = request.research_task
    point = request.novelty_point
    return EvidenceCard(
        card_id=f"CARD-{point.point_id}-{task.task_id}",
        task_id=task.task_id,
        novelty_point_id=point.point_id,
        document_title=f"Candidate {point.point_id} {task.task_id}",
        main_contribution="候选贡献",
        overlaps=["技术重合"],
        differences=["范围不同"],
        sources=[
            EvidenceSource(
                title=f"Candidate {point.point_id} {task.task_id}",
                quote="Grounded quote.",
                location="artifact chars:0-15",
                url="https://example.test/paper",
            )
        ],
        relevance=0.9,
        confidence=0.9,
        evidence_ids=[evidence_id],
    )


class RecordingTaskResearcher:
    def __init__(
        self,
        *,
        fail_task: str | None = None,
        first_round_empty=False,
        output_root: str | Path = "outputs",
    ):
        self.fail_task = fail_task
        self.first_round_empty = first_round_empty
        self.calls: list[TaskResearchRequest] = []
        self.active = 0
        self.max_active = 0
        self.reference_store = ReferenceStore(output_root)
        self.store_lock = threading.RLock()

    async def ainvoke(self, request: TaskResearchRequest) -> TaskResearchResult:
        self.calls.append(request)
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        await asyncio.sleep(0.01)
        self.active -= 1
        if request.research_task.task_id == self.fail_task:
            raise RuntimeError("task failed")
        cards = []
        evidence = []
        if not (self.first_round_empty and request.research_task.attempt == 1):
            task = request.research_task
            point = request.novelty_point
            quote = "Grounded quote."
            work_id = f"work-{point.point_id}-{task.task_id}"
            artifact_id = f"artifact-{point.point_id}-{task.task_id}"
            evidence_id = f"evidence-{point.point_id}-{task.task_id}"
            with self.store_lock:
                manifest = self.reference_store.load_manifest(
                    request.subject_paper_id
                )
                self.reference_store.write_document(
                    request.subject_paper_id,
                    work_id=work_id,
                    artifact_id=artifact_id,
                    extension="txt",
                    content=quote,
                )
                self.reference_store.persist_manifest(
                    request.subject_paper_id,
                    manifest.model_copy(
                        update={
                            "works": [
                                *manifest.works,
                                Work(
                                    work_id=work_id,
                                    work_type=WorkType.ARTICLE,
                                    title=f"Candidate {point.point_id} {task.task_id}",
                                ),
                            ],
                            "artifacts": [
                                *manifest.artifacts,
                                Artifact(
                                    artifact_id=artifact_id,
                                    work_id=work_id,
                                    role=ArtifactRole.EXTRACTED_TEXT,
                                    media_type="text/plain",
                                    relative_path=(
                                        f"documents/{work_id}/{artifact_id}.txt"
                                    ),
                                    sha256=hashlib.sha256(quote.encode()).hexdigest(),
                                    content_extent=ContentExtent.FULL,
                                    acquired_at=datetime.now(timezone.utc),
                                ),
                            ],
                            "updated_at": datetime.now(timezone.utc),
                        }
                    ),
                )
            evidence = [
                Evidence(
                    evidence_id=evidence_id,
                    work_id=work_id,
                    artifact_id=artifact_id,
                    novelty_point_id=point.point_id,
                    task_id=task.task_id,
                    quote=quote,
                    locator=EvidenceLocator(char_start=0, char_end=len(quote)),
                    interpretation="fixture",
                    confidence=0.9,
                )
            ]
            cards = [make_card(request, evidence_id)]
        return TaskResearchResult(
            task_id=request.research_task.task_id,
            novelty_point_id=request.novelty_point.point_id,
            status=TaskResearchStatus.COMPLETED,
            evidence=evidence,
            evidence_cards=cards,
            steps_used=1,
        )


class RecordingValidator:
    def __init__(self, researcher: RecordingTaskResearcher):
        self.researcher = researcher
        self.calls: list[tuple[int, int]] = []
        self.delegate = DefaultEvidenceValidator()

    def validate(self, cards, *, tasks):
        self.calls.append((len(cards), len(self.researcher.calls)))
        return self.delegate.validate(cards, tasks=tasks)


class RecordingPlanner(DemoSearchPlanner):
    def __init__(self):
        self.calls = []

    def plan(self, point, task):
        self.calls.append((point, task))
        return super().plan(point, task)


class AlwaysFailingPlanner:
    def plan(self, point, task):
        raise ValueError(f"planner failed for {point.point_id}/{task.task_id}")


def build_workflow(researcher=None, validator=None, planner=None, **config):
    output_root = config.pop("output_root", "outputs")
    researcher = researcher or RecordingTaskResearcher(output_root=output_root)
    return NoveltyWorkflow(
        NoveltyWorkflowServices(
            coordinator=DemoCoordinator(),
            task_researcher=researcher,
            search_planner=planner or DemoSearchPlanner(),
            point_extractor=DemoPointExtractor(),
            validator=validator,
        ),
        NoveltyWorkflowConfig(**config),
        output_root=output_root,
    ), researcher


def test_graph_replaces_fixed_retrieval_nodes():
    nodes = NoveltyWorkflow.default().graph.get_graph().nodes
    assert "dispatch_research_tasks" in nodes
    assert "run_research_task" in nodes
    assert "validate_evidence" in nodes
    assert "dispatch_planning_tasks" in nodes
    assert "plan_research_task" in nodes
    assert "plan_search" not in nodes
    assert "retrieve_candidates" not in nodes
    assert "parallel_research" not in nodes


def _final_evidence_sufficiency_state(card_count: int):
    paper = make_paper()
    point = DemoPointExtractor().extract(
        build_paper_digest(paper), previous_brief=None, attempt=1
    )[0]
    brief = DemoCoordinator().plan(paper, points=[point], attempt=1)
    task = brief.research_tasks[0]
    request = TaskResearchRequest(
        subject_paper_id=paper.paper_id,
        run_id="run-sufficiency-test",
        novelty_point=point,
        research_task=task,
        search_plan=DemoSearchPlanner().plan(point, task),
    )
    cards = [
        make_card(request).model_copy(update={"card_id": f"CARD-{index}"})
        for index in range(card_count)
    ]
    return {"paper": paper, "brief": brief, "evidence_cards": cards, "rounds": 1}


def test_final_evidence_sufficiency_passes_at_configured_cut():
    workflow, _ = build_workflow(
        min_final_evidence_cards_per_point=2,
        max_rounds=2,
    )
    checked = asyncio.run(
        workflow._check_final_evidence_sufficiency(
            _final_evidence_sufficiency_state(2)
        )
    )
    assert checked["insufficient_final_evidence_points"] == []
    assert asyncio.run(
        workflow._route_after_evidence_sufficiency_check(
            {**_final_evidence_sufficiency_state(2), **checked}
        )
    ) == "synthesize"


def test_final_evidence_sufficiency_records_count_below_cut_and_supplements():
    workflow, _ = build_workflow(
        min_final_evidence_cards_per_point=2,
        max_rounds=2,
    )
    state = _final_evidence_sufficiency_state(1)
    checked = asyncio.run(workflow._check_final_evidence_sufficiency(state))
    item = checked["insufficient_final_evidence_points"][0]
    assert item.novelty_point_id == "NP-1"
    assert item.valid_card_count == 1
    assert item.required_card_count == 2
    assert asyncio.run(
        workflow._route_after_evidence_sufficiency_check({**state, **checked})
    ) == "supplement"


def test_zero_cards_is_insufficient_final_evidence():
    workflow, _ = build_workflow(min_final_evidence_cards_per_point=2)
    checked = asyncio.run(
        workflow._check_final_evidence_sufficiency(
            _final_evidence_sufficiency_state(0)
        )
    )
    item = checked["insufficient_final_evidence_points"][0]
    assert item.valid_card_count == 0
    assert item.reason == "insufficient_final_evidence"


def test_final_evidence_sufficiency_stops_supplement_at_round_limit():
    workflow, _ = build_workflow(
        min_final_evidence_cards_per_point=2,
        max_rounds=1,
    )
    state = _final_evidence_sufficiency_state(0)
    checked = asyncio.run(workflow._check_final_evidence_sufficiency(state))
    assert checked["insufficient_final_evidence_points"]
    assert asyncio.run(
        workflow._route_after_evidence_sufficiency_check({**state, **checked})
    ) == "synthesize"


def test_final_evidence_cut_rejects_values_below_one():
    with pytest.raises(ValueError, match="min_final_evidence_cards_per_point"):
        NoveltyWorkflowConfig(min_final_evidence_cards_per_point=0)


def test_each_task_is_isolated_and_fan_out_runs_concurrently():
    planner = RecordingPlanner()
    workflow, researcher = build_workflow(planner=planner, max_concurrency=4)
    result = workflow.run(make_paper(claims=2))
    assert len(researcher.calls) == 4
    assert researcher.max_active > 1
    assert all(
        call.research_task.novelty_point_id == call.novelty_point.point_id
        for call in researcher.calls
    )
    assert len(result.evidence_cards) == 4
    assert len(planner.calls) == 4
    assert all(
        call.search_plan.task_id == call.research_task.task_id
        and call.search_plan.novelty_point_id == call.novelty_point.point_id
        for call in researcher.calls
    )


def _six_task_planning_state():
    paper = make_paper(claims=3)
    points = DemoPointExtractor().extract(
        build_paper_digest(paper), previous_brief=None, attempt=1
    )
    brief = DemoCoordinator().plan(paper, points=points, attempt=1)
    plans = [
        DemoSearchPlanner().plan(
            next(point for point in points if point.point_id == task.novelty_point_id),
            task,
        )
        for task in brief.research_tasks
    ]
    return {
        "paper": paper,
        "run_id": "run-plan-completeness",
        "novelty_points": points,
        "research_tasks": brief.research_tasks,
        "all_research_tasks": brief.research_tasks,
        "search_plans": plans,
        "rounds": 1,
    }


def test_six_tasks_five_plans_fails_closed_before_research_dispatch():
    workflow, _ = build_workflow()
    state = _six_task_planning_state()
    state["search_plans"] = state["search_plans"][:-1]

    with pytest.raises(WorkflowExecutionError, match="completeness check failed"):
        asyncio.run(workflow._dispatch_research_tasks(state))


def test_six_tasks_six_plans_dispatches_all_tasks():
    workflow, _ = build_workflow()
    state = _six_task_planning_state()

    sends = asyncio.run(workflow._dispatch_research_tasks(state))

    assert len(sends) == 6


def test_planner_failure_marks_runtime_failed(tmp_path):
    workflow, _ = build_workflow(planner=AlwaysFailingPlanner(), max_rounds=1)

    with pytest.raises(WorkflowExecutionError, match="completeness check failed"):
        workflow.run(make_paper())

    manifest_path = next(
        (tmp_path / "outputs" / "paper-test" / "runtime").glob("*/manifest.json")
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["status"] == "FAILED"


def test_sequential_runs_with_same_paper_id_do_not_overwrite(tmp_path):
    first_root = tmp_path / "runs" / "0001"
    second_root = tmp_path / "runs" / "0002"
    first, _ = build_workflow(output_root=first_root, max_rounds=1)
    second, _ = build_workflow(output_root=second_root, max_rounds=1)

    first.run(make_paper().model_copy(update={"title": "Run A"}))
    second.run(make_paper().model_copy(update={"title": "Run B"}))

    relative = Path("paper-test/paper-input/others/paper.json")
    first_paper = json.loads((first_root / relative).read_text(encoding="utf-8"))
    second_paper = json.loads((second_root / relative).read_text(encoding="utf-8"))
    assert first_paper["title"] == "Run A"
    assert second_paper["title"] == "Run B"
    assert not (tmp_path / "outputs" / "paper-test").exists()
    assert next((first_root / "paper-test/runtime").iterdir()).is_dir()
    assert (first_root / "paper-test/report/paper-test-report.md").is_file()


def test_validator_runs_once_after_current_round_fan_in():
    researcher = RecordingTaskResearcher()
    validator = RecordingValidator(researcher)
    workflow, _ = build_workflow(researcher, validator, max_rounds=1)
    workflow.run(make_paper())
    assert validator.calls == [(2, 2)]


def test_locator_gate_can_be_disabled_without_disabling_quote_gate():
    point = DemoPointExtractor().extract(
        build_paper_digest(make_paper()), previous_brief=None, attempt=1
    )[0]
    task = DemoCoordinator().plan(make_paper(), points=[point], attempt=1).research_tasks[0]
    request = TaskResearchRequest(
        subject_paper_id="paper-test",
        run_id="run-test",
        novelty_point=point,
        research_task=task,
        search_plan=DemoSearchPlanner().plan(point, task),
    )
    card = make_card(request)
    without_location = card.model_copy(
        update={"sources": [card.sources[0].model_copy(update={"location": None})]}
    )
    validator = DefaultEvidenceValidator(
        EvidenceValidationConfig(
            require_direct_quote=True,
            require_source_location=False,
        )
    )
    assert validator.validate([without_location], tasks=[task]).accepted == (
        without_location,
    )

    without_quote = without_location.model_copy(
        update={"sources": [without_location.sources[0].model_copy(update={"quote": None})]}
    )
    rejected = validator.validate([without_quote], tasks=[task]).rejected
    assert rejected == ((without_quote.card_id, "缺少原文摘录"),)


def test_locator_gate_remains_enabled_by_default():
    point = DemoPointExtractor().extract(
        build_paper_digest(make_paper()), previous_brief=None, attempt=1
    )[0]
    task = DemoCoordinator().plan(make_paper(), points=[point], attempt=1).research_tasks[0]
    request = TaskResearchRequest(
        subject_paper_id="paper-test",
        run_id="run-test",
        novelty_point=point,
        research_task=task,
        search_plan=DemoSearchPlanner().plan(point, task),
    )
    card = make_card(request)
    without_location = card.model_copy(
        update={"sources": [card.sources[0].model_copy(update={"location": None})]}
    )
    assert DefaultEvidenceValidator().validate(
        [without_location], tasks=[task]
    ).rejected == ((without_location.card_id, "缺少原文位置"),)


def test_single_task_failure_does_not_cancel_siblings():
    researcher = RecordingTaskResearcher(fail_task="T-1")
    workflow, _ = build_workflow(researcher, max_rounds=1)
    result = workflow.run(make_paper())
    assert len(researcher.calls) == 2
    assert result.evidence_cards
    assert any(issue.code == "research_task_failed" for issue in result.issues)


def test_supplement_dispatches_only_new_tasks():
    researcher = RecordingTaskResearcher(first_round_empty=True)
    planner = RecordingPlanner()
    workflow, _ = build_workflow(researcher, planner=planner, max_rounds=2)
    result = workflow.run(make_paper())
    assert result.rounds == 2
    assert [call.research_task.task_id for call in researcher.calls] == [
        "T-1",
        "T-2",
        "T-R2-1",
        "T-R2-2",
    ]
    assert result.evidence_cards
    assert len(planner.calls) == 4
    summary_path = next(Path("outputs/paper-test/runtime").glob("*/summary.json"))
    checks = json.loads(summary_path.read_text(encoding="utf-8"))[
        "final_evidence_sufficiency_checks"
    ]
    assert len(checks) == 2
    assert checks[0]["check_status"] == "INSUFFICIENT"
    assert checks[0]["actual_next_stage"] == "plan_supplement"
    assert checks[1]["check_status"] == "PASS"
    assert checks[1]["actual_next_stage"] == "synthesize_report"


class NoTaskCoordinator(DemoCoordinator):
    def plan(self, paper, *, points, attempt):
        brief = super().plan(paper, points=points, attempt=attempt)
        return brief.model_copy(update={"research_tasks": []})

    def plan_supplement(
        self,
        paper,
        *,
        brief,
        existing_evidence,
        insufficient_final_evidence_points,
        attempt,
    ):
        return brief.model_copy(update={"research_tasks": []})


def test_no_tasks_branch_does_not_hang():
    researcher = RecordingTaskResearcher()
    workflow = NoveltyWorkflow(
        NoveltyWorkflowServices(
            coordinator=NoTaskCoordinator(),
            task_researcher=researcher,
            search_planner=DemoSearchPlanner(),
            point_extractor=DemoPointExtractor(),
        ),
        NoveltyWorkflowConfig(max_rounds=1),
    )
    result = workflow.run(make_paper())
    assert researcher.calls == []
    assert result.insufficient_final_evidence_points
    summary_path = next(Path("outputs/paper-test/runtime").glob("*/summary.json"))
    check = json.loads(summary_path.read_text(encoding="utf-8"))[
        "final_evidence_sufficiency_checks"
    ][0]
    assert check["check_status"] == "INSUFFICIENT"
    assert check["round_limit_allows_supplement"] is False
    assert check["actual_next_stage"] == "synthesize_report"


def test_task_audit_and_compatibility_files_are_written():
    workflow, _ = build_workflow(max_rounds=1)
    workflow.run(make_paper())
    assert Path(
        "outputs/paper-test/research-runs/NP-1/T-1/attempt-1.json"
    ).is_file()
    retrieval = json.loads(
        Path("outputs/paper-test/retrieval-plans.json").read_text(encoding="utf-8")
    )
    assert retrieval["paper_id"] == "paper-test"
    assert all(plan["search_plans"] for plan in retrieval["novelty_point_plans"])
    assert Path("outputs/paper-test/evidence-cards.json").is_file()


def test_default_demo_runs_offline():
    result = NoveltyWorkflow.default().run(make_paper())
    assert result.rounds == 1
    assert result.evidence_cards


def test_trace_switch_keeps_offline_workflow_business_result(tmp_path):
    results = []
    for enabled in (False, True):
        root = tmp_path / str(enabled)
        workflow, _ = build_workflow(
            output_root=root,
            runtime_debug=RuntimeDebugConfig(
                enabled=enabled, output_root=root,
                archive_root=tmp_path / f"archive-{enabled}"),
        )
        results.append(workflow.run(make_paper()))
    assert [card.card_id for card in results[0].evidence_cards] == [
        card.card_id for card in results[1].evidence_cards]
    assert results[0].rounds == results[1].rounds
    assert results[0].report.model_dump(exclude={"generated_at"}) == results[1].report.model_dump(exclude={"generated_at"})
    archives = list((tmp_path / "archive-True").glob("paper-test_*/run-*"))
    assert len(archives) == 1
    archive = archives[0]
    assert list((archive / "stages").glob("*_run_research_task/input.json"))

    def check_refs(value):
        if isinstance(value, dict):
            if value.get("type") == "content_reference" and "path" in value:
                path = archive / value["path"]
                assert path.is_file()
                assert hashlib.sha256(path.read_bytes()).hexdigest() == value["sha256"]
            for child in value.values():
                check_refs(child)
        elif isinstance(value, list):
            for child in value:
                check_refs(child)

    for path in archive.rglob("*.json"):
        check_refs(json.loads(path.read_text()))


def test_planning_fan_out_respects_concurrency_and_fan_in_barrier():
    class AsyncPlanner(DemoSearchPlanner):
        def __init__(self):
            self.active = 0
            self.peak = 0
            self.calls = []
            self.completed = []
            self.first_pair = asyncio.Event()

        async def aplan(self, point, task):
            self.active += 1
            self.peak = max(self.peak, self.active)
            self.calls.append((point.point_id, task.task_id))
            if self.active == 2:
                self.first_pair.set()
            try:
                await self.first_pair.wait()
                await asyncio.sleep(0.01)
                plan = super().plan(point, task)
                self.completed.append((point.point_id, task.task_id))
                return plan
            finally:
                self.active -= 1

        def plan(self, point, task):
            raise AssertionError("Workflow must prefer async planning")

    planner = AsyncPlanner()
    workflow, researcher = build_workflow(planner=planner, max_concurrency=2, max_rounds=1)
    original = researcher.ainvoke
    async def research(request):
        assert len(planner.completed) == len(planner.calls)
        assert planner.active == 0
        return await original(request)
    researcher.ainvoke = research
    asyncio.run(asyncio.wait_for(workflow.arun(make_paper(claims=3)), timeout=10))
    assert planner.peak == 2
    assert len(planner.completed) == len(researcher.calls)
    assert len(planner.completed) >= 3
