from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from novelty_agent_framework.agents import DemoCoordinator
from novelty_agent_framework.core.integrity_gates import (
    validate_report_integrity,
    validate_synthesis_input,
)
from novelty_agent_framework.core.runtime_artifacts import RuntimeDebugConfig
from novelty_agent_framework.persistence import ReferenceStore, SubjectReferenceStore
from novelty_agent_framework.schemas import (
    Artifact,
    ArtifactRole,
    ContentExtent,
    Evidence,
    EvidenceCard,
    EvidenceLocator,
    EvidenceSource,
    NoveltyConclusion,
    NoveltyBrief,
    NoveltyPoint,
    NoveltyPointReview,
    NoveltyReport,
    NoveltyVerdict,
    PaperInput,
    RejectedEvidence,
    ResearchTask,
    ReviewStatus,
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


NOW = datetime(2026, 9, 6, tzinfo=timezone.utc)
TEXT = "prefix Alpha \t exact\nquote. suffix"
QUOTE = "Alpha exact quote."


def _point(point_id: str = "NP-1") -> NoveltyPoint:
    return NoveltyPoint(point_id=point_id, claim=f"claim {point_id}")


def _task(point_id: str = "NP-1", task_id: str = "T-1") -> ResearchTask:
    return ResearchTask(
        task_id=task_id,
        novelty_point_id=point_id,
        task_type="search",
        language="en",
    )


def _evidence(**updates) -> Evidence:
    values = {
        "evidence_id": "ev-1",
        "work_id": "work-1",
        "artifact_id": "artifact-1",
        "novelty_point_id": "NP-1",
        "task_id": "T-1",
        "quote": QUOTE,
        "locator": EvidenceLocator(char_start=7, char_end=27),
        "interpretation": "deterministic fixture",
        "confidence": 0.9,
    }
    values.update(updates)
    return Evidence(**values)


def _card(**updates) -> EvidenceCard:
    values = {
        "card_id": "card-1",
        "task_id": "T-1",
        "novelty_point_id": "NP-1",
        "document_title": "Work 1",
        "main_contribution": "contribution",
        "sources": [
            EvidenceSource(
                title="Work 1",
                quote=QUOTE,
                location="artifact artifact-1 chars:7-27",
                url="https://example.test/work-1",
            )
        ],
        "relevance": 0.9,
        "confidence": 0.9,
        "evidence_ids": ["ev-1"],
    }
    values.update(updates)
    return EvidenceCard(**values)


def _store(tmp_path: Path) -> tuple[ReferenceStore, Path]:
    store = ReferenceStore(tmp_path)
    path = store.write_document(
        "paper-1",
        work_id="work-1",
        artifact_id="artifact-1",
        extension="txt",
        content=TEXT,
    )
    manifest = store.load_manifest("paper-1")
    store.persist_manifest(
        "paper-1",
        manifest.model_copy(
            update={
                "works": [
                    Work(work_id="work-1", work_type=WorkType.ARTICLE, title="Work 1",
                         identifiers=[{"namespace": "url", "value": "https://example.test/work-1"}])
                ],
                "artifacts": [
                    Artifact(
                        artifact_id="artifact-1",
                        work_id="work-1",
                        role=ArtifactRole.EXTRACTED_TEXT,
                        media_type="text/plain",
                        relative_path="documents/work-1/artifact-1.txt",
                        sha256=hashlib.sha256(TEXT.encode()).hexdigest(),
                        byte_size=len(TEXT.encode()),
                        content_extent=ContentExtent.FULL,
                        acquired_at=NOW,
                    )
                ],
                "updated_at": NOW,
            }
        ),
    )
    return store, path


def _gate_a(tmp_path: Path, *, cards=None, evidence=None):
    store, path = _store(tmp_path)
    result = validate_synthesis_input(
        cards or [_card()],
        evidence=evidence or [_evidence()],
        tasks=[_task()],
        novelty_points=[_point()],
        paper_id="paper-1",
        reference_store=store,
    )
    return result, store, path


def _subject_store(tmp_path: Path) -> SubjectReferenceStore:
    """与 ``_store`` 相同的结构，但写入论文自带参考语料命名空间。"""

    store = SubjectReferenceStore(tmp_path)
    store.write_document(
        "paper-1",
        work_id="work-1",
        artifact_id="artifact-1",
        extension="txt",
        content=TEXT,
    )
    manifest = store.load_manifest("paper-1")
    store.persist_manifest(
        "paper-1",
        manifest.model_copy(
            update={
                "works": [
                    Work(work_id="work-1", work_type=WorkType.ARTICLE, title="Work 1",
                         identifiers=[{"namespace": "url", "value": "https://example.test/work-1"}])
                ],
                "artifacts": [
                    Artifact(
                        artifact_id="artifact-1",
                        work_id="work-1",
                        role=ArtifactRole.EXTRACTED_TEXT,
                        media_type="text/plain",
                        relative_path="documents/work-1/artifact-1.txt",
                        sha256=hashlib.sha256(TEXT.encode()).hexdigest(),
                        byte_size=len(TEXT.encode()),
                        content_extent=ContentExtent.FULL,
                        acquired_at=NOW,
                    )
                ],
                "updated_at": NOW,
            }
        ),
    )
    return store


def test_gate_a_accepts_evidence_from_the_subject_reference_namespace(
    tmp_path: Path,
) -> None:
    """回归守卫：来自论文自带参考语料的证据必须能通过 Gate A。

    Builder 按命名空间分别索引，而 Gate A 原先只加载研究语料那一份 Manifest，
    于是自带参考语料里的合法证据被误判为「缺失 Work/Artifact」——实测两张已经
    通过 Validator 的有效卡就这样被拒掉，直接拉低最终有效卡数量。
    """

    _store(tmp_path)  # 研究语料存在，但与本证据无关
    _subject_store(tmp_path)  # 证据真正所在的语料

    result = validate_synthesis_input(
        [_card()],
        evidence=[_evidence(provenance={"artifact_namespace": "subject_reference"})],
        tasks=[_task()],
        novelty_points=[_point()],
        paper_id="paper-1",
        reference_store=ReferenceStore(tmp_path),
    )

    assert result.rejected == ()
    assert result.accepted == (_card(),)


def test_gate_a_still_rejects_evidence_absent_from_its_own_namespace(
    tmp_path: Path,
) -> None:
    """命名空间感知不得放松校验：声称来自自带参考语料、但那里没有该制品，仍须拒绝。"""

    _store(tmp_path)  # 只写入研究语料

    result = validate_synthesis_input(
        [_card()],
        evidence=[_evidence(provenance={"artifact_namespace": "subject_reference"})],
        tasks=[_task()],
        novelty_points=[_point()],
        paper_id="paper-1",
        reference_store=ReferenceStore(tmp_path),
    )

    reasons = " ".join(result.rejected[0][1])
    assert "missing Work: work-1" in reasons
    assert "missing Artifact: artifact-1" in reasons


def test_gate_a_accepts_complete_chain_and_normalized_quote(tmp_path: Path) -> None:
    result, _store_value, _path = _gate_a(tmp_path)
    assert result.accepted == (_card(),)
    assert result.rejected == ()
    assert result.audit()["validation_passed"] is True


def test_gate_a_rejects_missing_and_cross_point_evidence(tmp_path: Path) -> None:
    missing, _store_value, _path = _gate_a(
        tmp_path / "missing", cards=[_card(evidence_ids=["ev-missing"])]
    )
    assert "missing Evidence: ev-missing" in missing.rejected[0][1]

    crossed, _store_value, _path = _gate_a(
        tmp_path / "crossed",
        evidence=[_evidence(novelty_point_id="NP-2")],
    )
    assert any("cross-point Evidence" in reason for reason in crossed.rejected[0][1])


def test_gate_a_rejects_missing_artifact(tmp_path: Path) -> None:
    result, _store_value, _path = _gate_a(
        tmp_path, evidence=[_evidence(artifact_id="artifact-missing")]
    )
    assert "missing Artifact: artifact-missing" in result.rejected[0][1]


def test_gate_a_rejects_identity_ambiguity_empty_refs_and_work_mismatch(
    tmp_path: Path,
) -> None:
    store, _path = _store(tmp_path)
    duplicate = _card(card_id="duplicate")
    result = validate_synthesis_input(
        [duplicate, duplicate, _card(card_id="empty", evidence_ids=[])],
        evidence=[
            _evidence(),
            _evidence(quote="conflicting object"),
        ],
        tasks=[_task()],
        novelty_points=[_point()],
        paper_id="paper-1",
        reference_store=store,
    )
    reasons = [reason for _card_value, values in result.rejected for reason in values]
    assert any("duplicate card_id" in reason for reason in reasons)
    assert any("evidence_ids is empty" in reason for reason in reasons)
    assert any("ambiguous Evidence" in reason for reason in reasons)

    mismatch = validate_synthesis_input(
        [_card()],
        evidence=[_evidence(work_id="work-missing")],
        tasks=[_task()],
        novelty_points=[_point()],
        paper_id="paper-1",
        reference_store=store,
    )
    mismatch_reasons = " ".join(mismatch.rejected[0][1])
    assert "missing Work: work-missing" in mismatch_reasons
    assert "Artifact/Evidence work mismatch" in mismatch_reasons


def test_validator_filtered_cards_drive_coverage_gap(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.chdir(tmp_path)
    store, _path = _store(tmp_path / "outputs")
    workflow = NoveltyWorkflow.default()
    workflow.reference_store = store
    point = _point()
    paper = PaperInput(paper_id="paper-1", title="Paper", full_text="body")
    cards = [
        _card(card_id="card-1", evidence_ids=["missing-1"]),
        _card(card_id="card-2", evidence_ids=["missing-2"]),
    ]
    state = {
        "paper": paper,
        "brief": NoveltyBrief(
            paper_summary="summary", novelty_points=[point], research_tasks=[_task()]
        ),
        "novelty_points": [point],
        "all_research_tasks": [_task()],
        "raw_evidence": [],
        "raw_evidence_cards": cards,
        "validator_accepted_cards": cards,
        "evidence_cards": cards,
        "rejected_evidence": [],
        "review_decisions": [],
        "rounds": 1,
    }
    gated = asyncio.run(workflow._validate_evidence(state))
    assert gated["evidence_cards"] == []
    assert len(gated["rejected_evidence"]) == 2
    sufficiency = asyncio.run(
        workflow._check_final_evidence_sufficiency({**state, **gated})
    )
    assert sufficiency["insufficient_final_evidence_points"]
    route = asyncio.run(
        workflow._route_after_evidence_sufficiency_check(
            {**state, **gated, **sufficiency}
        )
    )
    assert route == "supplement"


@pytest.mark.parametrize("damage", ["missing", "sha", "quote", "escape"])
def test_gate_a_rejects_artifact_and_locator_damage(
    tmp_path: Path, damage: str
) -> None:
    store, path = _store(tmp_path)
    evidence = _evidence()
    if damage == "missing":
        path.unlink()
    elif damage == "sha":
        path.write_text("tampered", encoding="utf-8")
    elif damage == "escape":
        outside = tmp_path / "outside.txt"
        outside.write_text(TEXT, encoding="utf-8")
        path.unlink()
        try:
            path.symlink_to(outside)
        except (OSError, NotImplementedError) as exc:
            # Windows 未开启开发者模式时没有创建符号链接的特权；manifest schema
            # 已拒绝 ../ 形式的 relative_path，因此无法用其它方式构造逃逸路径。
            pytest.skip(f"symlink creation is unavailable on this platform: {exc}")
    else:
        evidence = _evidence(quote="different quote")
    result = validate_synthesis_input(
        [_card()],
        evidence=[evidence],
        tasks=[_task()],
        novelty_points=[_point()],
        paper_id="paper-1",
        reference_store=store,
    )
    reasons = " ".join(result.rejected[0][1])
    expected = {
        "missing": "content file is missing",
        "sha": "sha256 mismatch",
        "quote": "quote/locator mismatch",
        "escape": "path escapes references workspace",
    }
    assert expected[damage] in reasons


def _report(conclusions: list[NoveltyConclusion]) -> NoveltyReport:
    return NoveltyReport(paper_id="paper-1", conclusions=conclusions)


def _conclusion(point_id="NP-1", supporting=None, counter=None):
    return NoveltyConclusion(
        novelty_point_id=point_id,
        review_status=ReviewStatus.REVIEWED,
        verdict=NoveltyVerdict.PARTIALLY_NOVEL,
        verdict_reason="review reason",
        summary="summary",
        supporting_card_ids=supporting or [],
        counter_card_ids=counter or [],
        confidence=0.5,
    )


def _review(point_id="NP-1", *, confidence=0.5):
    return NoveltyPointReview(
        novelty_point_id=point_id,
        status=ReviewStatus.REVIEWED,
        verdict=NoveltyVerdict.PARTIALLY_NOVEL,
        verdict_reason="review reason",
        confidence=confidence,
    )


def test_gate_b_accepts_complete_point_and_card_references() -> None:
    result = validate_report_integrity(
        _report([_conclusion(supporting=["card-1"])]),
        novelty_points=[_point()],
        evidence_cards=[_card()],
        novelty_reviews=[_review()],
    )
    assert result.validation_passed is True
    assert result.referenced_card_count == 1
    assert result.issues == ()


def test_gate_b_reports_missing_duplicate_and_unknown_conclusions() -> None:
    result = validate_report_integrity(
        _report([_conclusion(), _conclusion(), _conclusion("NP-99")]),
        novelty_points=[_point(), _point("NP-2")],
        evidence_cards=[],
        novelty_reviews=[_review(), _review("NP-2")],
    )
    assert result.validation_passed is False
    assert "duplicate conclusion: NP-1" in result.issues
    assert "missing conclusion: NP-2" in result.issues
    assert "unknown conclusion: NP-99" in result.issues


def test_gate_b_reports_unknown_cross_point_duplicates_and_conflict() -> None:
    card = _card(novelty_point_id="NP-2")
    result = validate_report_integrity(
        _report(
            [_conclusion(supporting=["missing", "card-1", "card-1"], counter=["card-1"])]
        ),
        novelty_points=[_point()],
        evidence_cards=[card],
        novelty_reviews=[_review()],
    )
    joined = "\n".join(result.issues)
    assert "unknown card reference: missing" in joined
    assert "cross-point reference: NP-1 -> card-1 (belongs to NP-2)" in joined
    assert "duplicate supporting card reference" in joined
    assert "supporting/counter conflict" in joined


def test_gate_b_reports_review_drift_and_unknown_review() -> None:
    conclusion = _conclusion().model_copy(update={"confidence": 0.9})
    result = validate_report_integrity(
        _report([conclusion]),
        novelty_points=[_point()],
        evidence_cards=[],
        novelty_reviews=[_review(), _review("NP-99")],
    )

    assert result.validation_passed is False
    assert "unknown review: NP-99" in result.issues
    assert any("fields=confidence" in issue for issue in result.issues)
    assert result.review_count == 2
    assert result.conclusion_count == 1
    assert result.matched_count == 0
    assert result.mismatch_count == 2
    assert result.mismatched_point_ids == ("NP-1", "NP-99")


def test_gate_b_failure_does_not_block_report_persistence(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    workflow = NoveltyWorkflow.default()
    paper = PaperInput(
        paper_id="paper-1",
        title="Paper",
        full_text="body",
        claimed_contributions=["claim"],
    )
    result = workflow.run(paper)
    persisted = json.loads(Path("outputs/paper-1/report.json").read_text(encoding="utf-8"))
    assert persisted == result.report.model_dump(mode="json")
    assert Path("outputs/paper-1/report/paper-1-report.md").is_file()

    gate_outputs = list(
        Path("outputs/paper-1/runtime").glob(
            "*/stages/*_validate_report_integrity/output.json"
        )
    )
    assert len(gate_outputs) == 1
    gate = json.loads(gate_outputs[0].read_text(encoding="utf-8"))
    assert gate["report_integrity"]["validation_passed"] is False
    gate_meta = json.loads((gate_outputs[0].parent / "meta.json").read_text(encoding="utf-8"))
    assert gate_meta["status"] == "SUCCESS"
    assert gate_meta["validation_result"]["validation_passed"] is False
    summaries = list(Path("outputs/paper-1/runtime").glob("*/summary.json"))
    summary = json.loads(summaries[0].read_text(encoding="utf-8"))
    gate_summaries = {
        item["stage_name"]: item for item in summary["integrity_gates"]
    }
    assert gate_summaries["validate_synthesis_input"]["validation_passed"] is True
    assert gate_summaries["validate_report_integrity"]["validation_passed"] is False


class _ValidReportCoordinator(DemoCoordinator):
    def synthesize(self, *args, **kwargs):
        report = super().synthesize(*args, **kwargs)
        return report.model_copy(
            update={
                "conclusions": [
                    conclusion.model_copy(update={"counter_card_ids": []})
                    for conclusion in report.conclusions
                ]
            }
        )


def test_complete_workflow_records_both_gate_passes(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    default = NoveltyWorkflow.default()
    workflow = NoveltyWorkflow(
        NoveltyWorkflowServices(
            coordinator=_ValidReportCoordinator(),
            task_researcher=default.services.task_researcher,
            search_planner=default.services.search_planner,
            point_extractor=default.services.point_extractor,
        ),
        NoveltyWorkflowConfig(max_rounds=1),
    )
    result = workflow.run(
        PaperInput(
            paper_id="valid-chain",
            title="Paper",
            full_text="body",
            claimed_contributions=["claim"],
        )
    )

    summary_path = next(
        Path("outputs/valid-chain/runtime").glob("*/summary.json")
    )
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    manifest = json.loads((summary_path.parent / "manifest.json").read_text(encoding="utf-8"))
    gate_results = {
        item["stage_name"]: item for item in summary["integrity_gates"]
    }
    stage_order = [
        item["stage_name"]
        for item in summary["stages"]
        if item["status"] != "NOT_RUN"
    ]
    assert summary["run"]["status"] == "SUCCESS"
    assert (
        manifest["runtime_config"]["workflow"]
        ["min_final_evidence_cards_per_point"]
        == 1
    )
    assert gate_results["validate_synthesis_input"]["validation_passed"] is True
    assert gate_results["validate_report_integrity"]["validation_passed"] is True
    assert gate_results["validate_report_integrity"]["review_count"] == 1
    assert gate_results["validate_report_integrity"]["conclusion_count"] == 1
    assert gate_results["validate_report_integrity"]["matched_count"] == 1
    assert gate_results["validate_report_integrity"]["mismatch_count"] == 0
    synthesize_input = json.loads(
        next(
            Path("outputs/valid-chain/runtime").glob(
                "*/stages/*_synthesize_report/input.json"
            )
        ).read_text(encoding="utf-8")
    )
    assert len(synthesize_input["novelty_reviews"]) == 1
    assert stage_order.index("review_evidence") < stage_order.index(
        "validate_synthesis_input"
    ) < stage_order.index("check_final_evidence_sufficiency")
    assert stage_order.index("synthesize_report") < stage_order.index(
        "validate_report_integrity"
    ) < stage_order.index("persist_report") < stage_order.index("render_report")
    assert result.evidence_cards
    assert json.loads(result.model_dump_json())[
        "insufficient_final_evidence_points"
    ] == []
    assert Path("outputs/valid-chain/report.json").is_file()
    assert Path("outputs/valid-chain/report/valid-chain-report.md").is_file()


def test_gate_a_reasons_do_not_enter_formal_report() -> None:
    workflow = NoveltyWorkflow.default()
    point = _point()
    paper = PaperInput(paper_id="paper-1", title="Paper", full_text="body")
    output = asyncio.run(
        workflow._synthesize_report(
            {
                "paper": paper,
                "novelty_points": [point],
                "brief": NoveltyBrief(
                    paper_summary="summary",
                    novelty_points=[point],
                    research_tasks=[_task()],
                ),
                "evidence_cards": [],
                "novelty_reviews": [_review()],
                "rejected_evidence": [
                    RejectedEvidence(
                        card_id="gate-only",
                        reason="missing Evidence: ev-missing",
                    )
                ],
                "integrity_rejected_card_ids": ["gate-only"],
                "insufficient_final_evidence_points": [],
            }
        )
    )
    # A missing execution audit remains an honest coverage limitation; Gate A
    # implementation details must still stay out of the formal report.
    assert output["report"].limitations == [
        "本报告节点输入未提供完整检索执行事实，检索覆盖状态未知。"
    ]
    rendered = output["report"].model_dump_json()
    assert "gate-only" not in rendered
    assert "ev-missing" not in rendered
    assert "missing Evidence" not in rendered


class _BrokenArtifactResearcher:
    def __init__(self) -> None:
        self.reference_store = ReferenceStore()

    async def ainvoke(self, request: TaskResearchRequest) -> TaskResearchResult:
        task = request.research_task
        point = request.novelty_point
        card = EvidenceCard(
            card_id=f"broken-{task.task_id}",
            task_id=task.task_id,
            novelty_point_id=point.point_id,
            document_title=f"Broken {task.task_id}",
            main_contribution="candidate contribution",
            sources=[
                EvidenceSource(
                    title=f"Broken {task.task_id}",
                    quote="quoted text",
                    location="chars:0-11",
                    url="https://example.test/broken",
                )
            ],
            relevance=0.9,
            confidence=0.9,
            evidence_ids=[f"evidence-{task.task_id}"],
        )
        evidence = Evidence(
            evidence_id=f"evidence-{task.task_id}",
            work_id=f"missing-work-{task.task_id}",
            artifact_id=f"missing-artifact-{task.task_id}",
            novelty_point_id=point.point_id,
            task_id=task.task_id,
            quote="quoted text",
            locator=EvidenceLocator(char_start=0, char_end=11),
            interpretation="corrupt fixture",
            confidence=0.9,
        )
        return TaskResearchResult(
            task_id=task.task_id,
            novelty_point_id=point.point_id,
            status=TaskResearchStatus.COMPLETED,
            evidence=[evidence],
            evidence_cards=[card],
            steps_used=1,
        )


def test_validator_provenance_failure_is_runtime_only_and_run_continues(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.chdir(tmp_path)
    default = NoveltyWorkflow.default()
    workflow = NoveltyWorkflow(
        NoveltyWorkflowServices(
            coordinator=_ValidReportCoordinator(),
            task_researcher=_BrokenArtifactResearcher(),
            search_planner=default.services.search_planner,
            point_extractor=default.services.point_extractor,
        ),
        NoveltyWorkflowConfig(max_rounds=1),
    )
    result = workflow.run(
        PaperInput(
            paper_id="broken-chain",
            title="Paper",
            full_text="body",
            claimed_contributions=["claim"],
        )
    )

    assert result.evidence_cards == []
    assert result.insufficient_final_evidence_points
    assert Path("outputs/broken-chain/report.json").is_file()
    assert Path("outputs/broken-chain/report/broken-chain-report.md").is_file()
    summary_path = next(
        Path("outputs/broken-chain/runtime").glob("*/summary.json")
    )
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    gate_a = next(
        item
        for item in summary["integrity_gates"]
        if item["stage_name"] == "validate_evidence"
    )
    assert summary["run"]["status"] == "SUCCESS"
    assert gate_a["validation_passed"] is False
    assert gate_a["rejected_card_count"] == 2
    assert all(
        any("missing Artifact" in reason for reason in rejected["reasons"])
        for rejected in gate_a["rejected_cards"]
    )
    formal_report = Path("outputs/broken-chain/report.json").read_text(encoding="utf-8")
    assert "missing Artifact" not in formal_report


def test_runtime_debug_off_still_filters_broken_gate_a_input(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.chdir(tmp_path)
    default = NoveltyWorkflow.default()
    workflow = NoveltyWorkflow(
        NoveltyWorkflowServices(
            coordinator=_ValidReportCoordinator(),
            task_researcher=_BrokenArtifactResearcher(),
            search_planner=default.services.search_planner,
            point_extractor=default.services.point_extractor,
        ),
        NoveltyWorkflowConfig(
            max_rounds=1,
            runtime_debug=RuntimeDebugConfig(enabled=False),
        ),
    )
    result = workflow.run(
        PaperInput(
            paper_id="broken-debug-off",
            title="Paper",
            full_text="body",
            claimed_contributions=["claim"],
        )
    )

    assert result.evidence_cards == []
    assert any("missing Artifact" in item.reason for item in result.rejected_evidence)
    assert Path("outputs/broken-debug-off/report.json").is_file()
    assert not Path("outputs/broken-debug-off/runtime").exists()


def test_workflow_contains_integrity_and_persistence_stages() -> None:
    nodes = NoveltyWorkflow.default().graph.get_graph().nodes
    assert "validate_synthesis_input" in nodes
    assert "validate_report_integrity" in nodes
    assert "persist_report" in nodes


def test_runtime_debug_off_keeps_integrity_gates_and_formal_outputs(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.chdir(tmp_path)
    default = NoveltyWorkflow.default()
    workflow = NoveltyWorkflow(
        default.services,
        NoveltyWorkflowConfig(
            runtime_debug=RuntimeDebugConfig(enabled=False)
        ),
    )
    paper = PaperInput(
        paper_id="debug-off",
        title="Paper",
        full_text="body",
        claimed_contributions=["claim"],
    )
    result = workflow.run(paper)
    assert result.evidence_cards
    assert Path("outputs/debug-off/report.json").is_file()
    assert Path("outputs/debug-off/report/debug-off-report.md").is_file()
    assert not Path("outputs/debug-off/runtime").exists()
