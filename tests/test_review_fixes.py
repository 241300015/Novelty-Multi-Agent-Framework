"""Regression cases for the five October review findings."""
import asyncio
import json

import pytest

from test_correction_trace import _runtime, _scope, Tool, FailingTool
from novelty_agent_framework.tools import ResearcherToolRegistry
from novelty_agent_framework.core.correction_trace import record_detection, record_advice, record_execution
from novelty_agent_framework.schemas.failures import (
    CorrectionBudgetSnapshot, CorrectionStatus, FailureCode, FailureScope,
    RecoveryAction, advance_correction, make_failure,
)


def started():
    record = record_detection(make_failure(FailureCode.TOOL_ARGUMENT_SCHEMA,
        scope=FailureScope(run_id="run-1", paper_id="paper-1")))
    return record_advice(record, RecoveryAction.REPAIR_ARGUMENTS)


def test_empty_budget_preserves_known_values(tmp_path):
    manager = _runtime(tmp_path)
    manager.activate()
    try:
        record = record_execution(started(), attempt=1, budget={
            "local_limit": 1, "local_used": 1, "global_limit": 10, "global_used": 4,
            "supplement_round_before": 1, "supplement_round_after": 1})
        next_record = advance_correction(record, CorrectionStatus.VERIFICATION_PENDING,
            budget=CorrectionBudgetSnapshot())
        saved = manager.record_correction(next_record)
        assert saved.budget.global_used == 4
        assert saved.budget.supplement_round_after == 1
        assert manager.correction_index()["records"][0]["budget"]["global_limit"] == 10
    finally:
        manager.deactivate()


@pytest.mark.parametrize("attempt", [2, 99])
def test_attempt_cannot_exceed_catalog(attempt):
    with pytest.raises(ValueError, match="limit"):
        record_execution(started(), attempt=attempt)


def test_returned_failure_without_pending_is_audited(tmp_path):
    manager = _runtime(tmp_path)
    manager.activate()
    try:
        observation = asyncio.run(ResearcherToolRegistry([FailingTool()]).execute(
            "example", {"value": "valid"}, scope=_scope()))
        assert observation.succeeded is False
        index = manager.correction_index()
        assert len(index["records"]) == 1
        assert index["records"][0]["attempt"] == 0
        assert index["records"][0]["status"] == "stopped"
        assert observation.payload["failure"]["code"] == "execution.unknown"
    finally:
        manager.deactivate()


def test_repeated_invalid_arguments_stop_original_chain(tmp_path):
    class CountingTool(Tool):
        calls = 0
        async def ainvoke(self, arguments, *, scope):
            self.calls += 1
            return await super().ainvoke(arguments, scope=scope)
    tool = CountingTool()
    manager = _runtime(tmp_path)
    registry = ResearcherToolRegistry([tool])
    async def run():
        for args in ({"bad": 1}, {"still_bad": 2}, {"value": "ok"}):
            await registry.execute("example", args, scope=_scope())
    manager.activate()
    try:
        asyncio.run(run())
        index = manager.correction_index()
        assert len(index["records"]) == 1
        assert index["records"][0]["status"] == "stopped"
        assert index["records"][0]["attempt"] == 1
        assert index["unresolved_correction_ids"] == []
        assert tool.calls == 0
    finally:
        manager.deactivate()


def test_read_selection_does_not_replay_all_body(tmp_path):
    from test_point_paper_reading import prepare, TailClient
    from novelty_agent_framework.agents import NoveltyPointExtractorAgent
    _, ref, digest = prepare(tmp_path)
    client = TailClient(ref)
    agent = NoveltyPointExtractorAgent(client)
    agent.extract(digest, previous_brief=None, attempt=1)
    # Raw text belongs to the one segment processing request, not review.
    raw = client.reader.read(client.segment)["text"]
    assert json.dumps(raw, ensure_ascii=False)[1:-1] not in client.calls[-1]


@pytest.mark.parametrize("field", ["local_limit", "local_used", "global_limit", "global_used",
    "supplement_round_before", "supplement_round_after"])
def test_explicit_null_cannot_erase_budget(field):
    record = record_execution(started(), attempt=1, budget={"local_limit": 1, "local_used": 1,
        "global_limit": 10, "global_used": 4, "supplement_round_before": 1, "supplement_round_after": 1})
    with pytest.raises(ValueError, match="erase"):
        advance_correction(record, CorrectionStatus.VERIFICATION_PENDING, budget={field: None})


@pytest.mark.parametrize("patch", [{"local_used": 0}, {"global_used": 3}, {"local_limit": 2},
    {"global_limit": 11}, {"supplement_round_before": 0}, {"supplement_round_after": 2}])
def test_budget_changes_cannot_reset_contract(patch):
    record = record_execution(started(), attempt=1, budget={"local_limit": 1, "local_used": 1,
        "global_limit": 10, "global_used": 4, "supplement_round_before": 1, "supplement_round_after": 1})
    with pytest.raises(ValueError):
        advance_correction(record, CorrectionStatus.VERIFICATION_PENDING, budget=patch)


def test_zero_retry_category_cannot_execute():
    record = record_detection(make_failure(FailureCode.TOOL_NOT_REGISTERED, scope=FailureScope()))
    record = record_advice(record, RecoveryAction.MANUAL)
    with pytest.raises(ValueError, match="limit"):
        record_execution(record, attempt=1)


def test_zero_local_limit_cannot_execute():
    with pytest.raises(ValueError, match="local limit"):
        record_execution(started(), attempt=1, budget={"local_limit": 0})


def test_scope_round_isolation_and_concurrent_repairs(tmp_path):
    manager = _runtime(tmp_path)
    registry = ResearcherToolRegistry([Tool()])
    async def run():
        first = _scope()
        second = first.model_copy(update={"research_task": first.research_task.model_copy(update={"attempt": 2})})
        await registry.execute("example", {"bad": 1}, scope=first)
        await registry.execute("example", {"bad": 2}, scope=first)
        assert (await registry.execute("example", {"value": "new round"}, scope=second)).succeeded
        # Different tools/tasks must not accidentally consume the stopped chain.
        other = first.model_copy(update={"research_task": first.research_task.model_copy(update={"task_id": "T-2"})})
        results = await asyncio.gather(*(registry.execute("example", {"bad": n}, scope=other) for n in range(3)))
        assert all(not item.succeeded for item in results)
    manager.activate()
    try:
        asyncio.run(run())
        records = manager.correction_index()["records"]
        assert len(records) == 2
        assert all(row["status"] == "stopped" for row in records)
    finally:
        manager.deactivate()


def test_full_reading_24_chinese_segments_has_bounded_requests(tmp_path):
    from backend.env import ModelResponse, ModelProfile, OpenAICompatibleChatClient, ContextTokenMeasurement
    from novelty_agent_framework.agents import NoveltyPointExtractorAgent, build_paper_digest
    from novelty_agent_framework.persistence import persist_workflow_input
    from novelty_agent_framework.schemas import PaperInput
    paper = PaperInput(paper_id="long", title="长文", abstract="方法介绍", full_text="技术机制" * 24000)
    _, ref = persist_workflow_input(paper, output_root=tmp_path, with_reading_reference=True)
    from novelty_agent_framework.core.paper_reading import PaperReader
    locations = [PaperReader(ref).read(f"body-{i}")["source_location"] for i in range(1, 25)]
    class Client(OpenAICompatibleChatClient):
        def __init__(self):
            super().__init__(ModelProfile(alias="test", model="test", context_window=32768),
                token_counter=lambda payload: ContextTokenMeasurement(
                    sum(len(m["content"]) for m in payload["messages"]), "fixture_character_tokenizer"))
            self.calls = []
            self.selected = 0
        def _complete(self, messages, *, options=None):
            self.calls.append(messages[-1].content)
            text = messages[-1].content
            if '不超过240字符' in text:
                result = {"summary": "技术机制的有界笔记"}
            elif 'available_segments' in text:
                self.selected += 1
                result = {"segment_id": f"body-{self.selected}"}
            elif '判断重复项' in text:
                result = {"deletions": []}
            else:
                result = {"novelty_points": [{"point_id": str(i), "claim": f"机制{i}",
                    "technical_features": [str(i)], "source_locations": [locations[-1]]} for i in range(3)]}
            return ModelResponse(content=json.dumps(result))
    client = Client()
    agent = NoveltyPointExtractorAgent(client)
    points = agent.extract(build_paper_digest(paper).model_copy(update={"paper_source": ref}), previous_brief=None, attempt=1)
    assert len(points) == 3
    assert len(agent.last_trace["reading"]["reads"]) == 24
    assert len(agent.last_trace["reading"]["notes"]) == 24
    assert len(client.calls) == 50  # 24 choices + 24 notes + generation + review
    assert max(map(len, client.calls)) < 24576
    assert "技术机制" * 100 not in client.calls[-1]
    selection = [text for text in client.calls if 'available_segments' in text]
    assert max(map(len, selection)) < 15000


@pytest.mark.parametrize("counter_code", ["limit", "unavailable"])
def test_point_reading_admission_fails_before_chat(tmp_path, monkeypatch, counter_code):
    from backend.env import ModelProfile, OpenAICompatibleChatClient, ContextTokenMeasurement, ModelContextAdmissionError
    from novelty_agent_framework.agents import NoveltyPointExtractorAgent
    from test_point_paper_reading import prepare
    def count(payload):
        if counter_code == "unavailable":
            raise ValueError("counter unavailable")
        return ContextTokenMeasurement(999999, "fixture")
    client = OpenAICompatibleChatClient(ModelProfile(alias="test", model="test"), token_counter=count)
    monkeypatch.setattr(client, "_complete", lambda *a, **k: pytest.fail("chat must not be sent"))
    _, _, digest = prepare(tmp_path)
    agent = NoveltyPointExtractorAgent(client)
    with pytest.raises(ModelContextAdmissionError):
        agent.extract(digest, previous_brief=None, attempt=1)
    expected = "model.context_limit" if counter_code == "limit" else "model.context_unavailable"
    assert agent.last_trace["failures"][-1]["code"] == expected


@pytest.mark.parametrize("failed", [True, False])
def test_database_failure_has_owner_code_and_audit(tmp_path, failed):
    from test_database_search_tool import observe_bundle, bundle
    from novelty_agent_framework.schemas import SearchExecutionStatus
    tool, observation = observe_bundle(tmp_path, bundle(*([SearchExecutionStatus.FAILED] if failed else [])))
    expected = "provider.unknown" if failed else "coverage.not_executed"
    assert observation.payload["failure_code"] == expected
    from novelty_agent_framework.tools.researcher_registry import _audit_failed_observation
    manager = _runtime(tmp_path)
    manager.activate()
    try:
        from novelty_agent_framework.schemas import DatabaseSearchArguments
        audited = _audit_failed_observation(observation, _scope(), tool.name, DatabaseSearchArguments(source_id="demo"))
        assert audited.payload["failure"]["code"] == expected
        assert len(manager.correction_index()["records"]) == 1
    finally:
        manager.deactivate()


def test_baidu_rejected_query_audited_without_network(tmp_path):
    from novelty_agent_framework.tools import WebSearchTool, BaiduSearchBackend
    from novelty_agent_framework.persistence import ReferenceStore
    tool = WebSearchTool(BaiduSearchBackend(api_key="fixture"), reference_store=ReferenceStore(tmp_path / "store"))
    manager = _runtime(tmp_path)
    manager.activate()
    try:
        observation = asyncio.run(ResearcherToolRegistry([tool]).execute("web_search", {"query": "测" * 100}, scope=_scope()))
        assert not observation.succeeded
        assert observation.payload["failure"]["code"] == "tool.arguments.schema_invalid"
        assert len(manager.correction_index()["records"]) == 1
    finally:
        manager.deactivate()
