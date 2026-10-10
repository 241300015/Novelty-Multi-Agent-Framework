"""论文查新可选 API 的任务生命周期测试。"""

from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from novelty_agent_framework.config import NoveltyWebSettings
from novelty_agent_framework.main import create_app
from novelty_agent_framework.schemas import PaperInput
from novelty_agent_framework.services import NoveltyWorkflowService
from novelty_agent_framework.services.jobs import InMemoryRunStore, RunStage
from novelty_agent_framework.workflows import NoveltyWorkflow

ROOT = Path(__file__).resolve().parents[1]


def load_example(name: str) -> dict:
    return json.loads((ROOT / "examples" / name).read_text(encoding="utf-8"))


def demo_app(tmp_path: Path):
    settings = NoveltyWebSettings(workflow_mode="demo", runs_root=tmp_path)
    return create_app(settings)


def test_novelty_health_and_run_lifecycle(tmp_path: Path) -> None:
    with TestClient(demo_app(tmp_path)) as client:
        health = client.get("/api/novelty/health")
        assert health.status_code == 200
        assert health.json()["status"] == "ready"
        assert health.json()["workflow"] == "demo"

        created = client.post(
            "/api/novelty/runs",
            json=load_example("paper.json"),
        )
        assert created.status_code == 202
        task_id = created.json()["task_id"]

        result = client.get(f"/api/novelty/runs/{task_id}")
        assert result.status_code == 200
        assert result.json()["status"] == "succeeded"
        assert result.json()["result"]["report"]["paper_id"] == "demo-paper-001"
        assert result.json()["progress"]["stage"] == "render_report"
        report = client.get(result.json()["report"]["preview_url"])
        assert report.status_code == 200
        assert report.headers["content-type"].startswith("text/markdown")


def test_api_validates_input_and_returns_not_found(tmp_path: Path) -> None:
    with TestClient(demo_app(tmp_path)) as novelty_client:
        invalid = novelty_client.post(
            "/api/novelty/runs", json={"title": "缺少字段"}
        )
        assert invalid.status_code == 422
        assert novelty_client.get("/api/novelty/runs/not-found").status_code == 404


def test_only_current_task_api_is_started(tmp_path: Path) -> None:
    app = demo_app(tmp_path)
    with TestClient(app) as client:
        assert not hasattr(app.state, "pdf_runs")
        assert client.get("/api/novelty/health").status_code == 200
        assert client.get("/api/health").status_code == 404
        assert "/api/runs" not in {route.path for route in app.routes}
        assert client.post("/api/runs").status_code in {404, 405}
        assert client.get("/api/runs/unknown").status_code == 404


def test_real_mode_fails_closed_without_credentials(tmp_path: Path, monkeypatch) -> None:
    for name in ("SILICONFLOW_API_KEY", "NOVELTY_API_KEY", "LLM_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    settings = NoveltyWebSettings(workflow_mode="real", runs_root=tmp_path)
    with TestClient(create_app(settings)) as client:
        health = client.get("/api/novelty/health")
        assert health.json()["status"] == "degraded"
        assert health.json()["workflow"] == "unavailable"
        unavailable = client.get("/api/novelty/runs/not-found")
        assert unavailable.status_code == 503
        assert unavailable.json()["detail"]["code"] == "workflow_unavailable"


def test_progress_shows_actual_research_stage_during_supplement() -> None:
    store = InMemoryRunStore()
    task_id = store.create().task_id
    store.mark_running(task_id, stage=RunStage.RESEARCH)
    store.mark_progress(task_id, RunStage.VALIDATE_EVIDENCE, round=1)
    snapshot = store.mark_progress(task_id, RunStage.RESEARCH, round=2)
    assert snapshot.progress is not None
    assert snapshot.progress.stage is RunStage.RESEARCH
    assert snapshot.progress.round == 2


class FakeProcessor:
    def process(self, _path: Path, *, paper_id: str) -> PaperInput:
        return PaperInput(paper_id=paper_id, title="上传论文", full_text="正文")

    def to_paper_input(self, document: PaperInput) -> PaperInput:
        return document


def test_file_run_and_references_boundary(tmp_path: Path) -> None:
    settings = NoveltyWebSettings(workflow_mode="demo", runs_root=tmp_path)
    service = NoveltyWorkflowService(
        workflow_factory=lambda root: NoveltyWorkflow.default(output_root=root),
        processor=FakeProcessor(),
        runs_root=tmp_path,
    )
    with TestClient(create_app(settings, service=service)) as client:
        rejected = client.post(
            "/api/novelty/runs/files",
            files=[
                ("paper", ("paper.pdf", b"%PDF-1.4\n", "application/pdf")),
                ("references", ("ref.txt", b"reference", "text/plain")),
            ],
        )
        assert rejected.status_code == 422
        assert rejected.json()["detail"]["code"] == "references_not_supported"

        created = client.post(
            "/api/novelty/runs/files",
            files={"paper": ("paper.pdf", b"%PDF-1.4\n", "application/pdf")},
        )
        assert created.status_code == 202
        task_id = created.json()["task_id"]
        snapshot = client.get(f"/api/novelty/runs/{task_id}").json()
        assert snapshot["status"] == "succeeded"
        assert snapshot["report"]["available_formats"] == ["md"]

        invalid = client.post(
            "/api/novelty/runs/files",
            files={"paper": ("paper.pdf", b"not a pdf", "application/pdf")},
        )
        assert invalid.status_code == 422
        assert invalid.json()["detail"]["code"] == "invalid_pdf_signature"


def test_same_file_submission_id_does_not_start_second_business_run(tmp_path: Path) -> None:
    settings = NoveltyWebSettings(workflow_mode="demo", runs_root=tmp_path)
    service = NoveltyWorkflowService(
        workflow_factory=lambda root: NoveltyWorkflow.default(output_root=root),
        processor=FakeProcessor(), runs_root=tmp_path,
    )
    executions: list[str] = []

    async def count_execution(task_id: str, _path: Path) -> None:
        executions.append(task_id)

    service.execute_file = count_execution  # type: ignore[method-assign]
    with TestClient(create_app(settings, service=service)) as client:
        def submit(content: bytes):
            return client.post("/api/novelty/runs/files",
                headers={"X-Submission-Id": "same-user-intent"},
                files={"paper": ("paper.pdf", content, "application/pdf")})

        first = submit(b"%PDF-1.4\nfirst")
        repeat = submit(b"%PDF-1.4\nfirst")
        assert first.status_code == repeat.status_code == 202
        assert first.json()["task_id"] == repeat.json()["task_id"]
        assert executions == [first.json()["task_id"]]
        assert len(list(tmp_path.glob("*/input/paper.pdf"))) == 1
        conflict = submit(b"%PDF-1.4\nother")
        assert conflict.status_code == 409
        assert conflict.json()["detail"]["code"] == "submission_conflict"
        assert executions == [first.json()["task_id"]]


def test_file_upload_rejects_empty_oversize_mime_and_multiple_papers(tmp_path: Path) -> None:
    settings = NoveltyWebSettings(workflow_mode="demo", runs_root=tmp_path)
    service = NoveltyWorkflowService(
        workflow_factory=lambda root: NoveltyWorkflow.default(output_root=root),
        processor=FakeProcessor(), runs_root=tmp_path, max_upload_bytes=10,
    )
    with TestClient(create_app(settings, service=service)) as client:
        cases = [
            ([('paper', ('paper.pdf', b'', 'application/pdf'))], 'empty_file'),
            ([('paper', ('paper.pdf', b'%PDF-1.4\n123', 'application/pdf'))], 'file_too_large'),
            ([('paper', ('paper.pdf', b'%PDF-1.4', 'text/plain'))], 'mime_mismatch'),
            ([('paper', ('a.pdf', b'%PDF-1.4', 'application/pdf')),
              ('paper', ('b.pdf', b'%PDF-1.4', 'application/pdf'))], 'single_pdf_required'),
        ]
        for files, code in cases:
            response = client.post('/api/novelty/runs/files', files=files)
            assert response.status_code in {413, 422}
            assert response.json()['detail']['code'] == code
        assert not list(tmp_path.glob('*/input/paper.pdf'))
