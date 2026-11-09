"""The live runner must enforce its cap at the actual HTTP dispatch boundary."""
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.run_planner_concept_case import run


def test_live_runner_stops_after_one_real_dispatch(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCAL_VLLM_API_KEY", "test-only")
    sent = []

    class Reply:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self):
            content = json.dumps({"concepts": [{"role": "method", "terms": ["图摘要"]}],
                                  "comparison": []}, ensure_ascii=False)
            return json.dumps({"id": "reply", "choices": [{"message": {"content": content}}]}).encode()

    def fake_urlopen(request, timeout):
        sent.append(request.full_url)
        return Reply()

    monkeypatch.setattr("backend.env.model_client.urllib.request.urlopen", fake_urlopen)
    source = Path("docs/experiments/20260915_224229/run/MF2033k6lC/novelty-points.json")
    args = SimpleNamespace(source=source, point="NP-1", scenario="A", repeat=1,
                           output=tmp_path, inject=None, target=None, alias=None,
                           destination=None, max_model_calls=1)
    with pytest.raises(Exception, match="model_call_budget_exhausted"):
        asyncio.run(run(args))
    assert sent == ["http://127.0.0.1:8000/v1/chat/completions"]
    summary_path, = tmp_path.glob("planner-attempts/*/planner/summary.json")
    summary = json.loads(summary_path.read_text())
    assert summary["status"] == "failed"
    assert "model_call_budget_exhausted" in summary["error"]
    runtime_calls = list(tmp_path.glob("*/runtime/*/llm_calls/*.json"))
    assert len(runtime_calls) == 1
