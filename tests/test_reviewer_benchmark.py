"""Benchmark integrity and metric arithmetic, not model-quality tests."""
from novelty_agent_framework.experiments.reviewer_benchmark import load_cases, metrics, run
from novelty_agent_framework.agents import DefaultEvidenceValidator


def test_real_source_cases_are_grounded_and_cover_gates():
    data, cases = load_cases()
    assert data["annotation_status"] == "agent_reviewed_pending_human"
    assert {c[0]["expected_verdict"] for c in cases} == {None, "accept", "reject", "needs_more_evidence"}
    for item, card, _, task, source_hash in cases:
        result = DefaultEvidenceValidator().validate_fields([card], tasks=[task])
        assert bool(result.accepted) == (item["expected_validator"] == "accept")
        assert len(source_hash) == 64


def test_runtime_errors_are_not_semantic_rejections():
    rows = [
        {"expected_verdict": "accept", "actual_verdict": "reject", "confidence_in_range": False},
        {"expected_verdict": "accept", "actual_verdict": "reject", "runtime_error": "timeout"},
        {"expected_verdict": "reject", "actual_verdict": "accept", "confidence_in_range": True},
        {"expected_verdict": "reject", "actual_verdict": "needs_more_evidence", "confidence_in_range": True},
        {"expected_verdict": "needs_more_evidence", "actual_verdict": "needs_more_evidence", "confidence_in_range": True},
        {"expected_verdict": None, "actual_verdict": None},
    ]
    result = metrics(rows)
    assert result["valid_decisions"] == 4
    assert result["runtime_errors"] == 1
    assert result["false_reject_count"] == result["false_reject_denominator"] == 1
    assert result["missed_reject_count"] == result["missed_reject_denominator"] == 2
    assert result["unsafe_accept_count"] == 1
    assert result["agreement"] == 0.25


def test_offline_mode_does_not_claim_quality_measurements(tmp_path):
    result = run(tmp_path / "offline", prompt_name="reviewer/review_evidence_candidate")
    assert result["metrics"]["agreement"] is None
    assert result["metrics"]["valid_decisions"] == 0
    assert result["validator_rejected"] == 2
    assert result["human_gold_metrics"] is False


def test_offline_mode_supports_default_point_reviewer_prompt(tmp_path):
    result = run(tmp_path / "offline-default")
    assert result["mode"] == "fixtures_only"
    assert result["metrics"]["runtime_errors"] == 0
    assert (tmp_path / "offline-default" / "summary.json").is_file()
    assert (tmp_path / "offline-default" / "report.md").is_file()
