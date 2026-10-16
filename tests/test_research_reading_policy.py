"""Guided selection must constrain actual I/O, not merely advise the model."""
import asyncio
import json
import subprocess
import sys
from types import SimpleNamespace

import pytest

from backend.env import ModelResponse, ModelToolCall
from novelty_agent_framework.core.tool_call_harness import ToolCallHarness, ToolCallHarnessConfig
from novelty_agent_framework.schemas import DatabaseSearchArguments, ResearcherToolObservation
from novelty_agent_framework.schemas.research_tools import ReaderCallArguments
from novelty_agent_framework.tools import ResearcherToolRegistry
from novelty_agent_framework.workflows.research_reading_policy import ResearchReadingPolicy


class Database:
    name = "database_search"
    description = "test database"
    args_schema = DatabaseSearchArguments
    tools_by_source = {"arxiv": object()}

    async def ainvoke(self, arguments, *, scope):
        acquisition = bool(arguments.full_text_source_record_ids)
        records = [{"source_record_id": f"src_{i}", "work_id": f"wrk_{i}",
                    "source_id": "arxiv", "title": f"Paper {i}"} for i in (1, 2)]
        artifacts = [{"artifact_id": "body_1", "source_record_id": "src_1",
                      "role": "extracted_text"}] if acquisition else [
            {"artifact_id": f"abstract_{i}", "source_record_id": f"src_{i}",
             "role": "abstract"} for i in (1, 2)]
        return ResearcherToolObservation(tool_name=self.name, succeeded=True,
            payload={"research_bundle": {"source_records": records, "artifacts": artifacts}})


class Reader:
    name = "reader"
    description = "test reader"
    args_schema = ReaderCallArguments

    def __init__(self):
        self.calls = []

    async def ainvoke(self, arguments, *, scope):
        self.calls.append(arguments)
        start = arguments.char_start
        return ResearcherToolObservation(tool_name=self.name, succeeded=True, payload={"read_result": {
            "artifact_id": arguments.artifact_id, "text": "verified text", "char_start": start,
            "char_end": start + 13, "has_more": arguments.artifact_id == "body_1" and start == 0}})


def choose(policy, contains):
    definition = policy.definitions()[0]
    labels = json.loads(definition.description.split("选项：", 1)[1])
    selected = next(key for key, label in labels.items() if contains in label)
    return ModelToolCall(id="choice", name=definition.name, arguments={"option_id": selected})


def test_menu_transitions_keep_handles_owned_by_runtime():
    async def run():
        reader = Reader()
        registry = ResearcherToolRegistry([Database(), reader])
        policy = ResearchReadingPolicy(registry, SimpleNamespace(recovery=None))
        async def step(label):
            selected = choose(policy, label)
            call = policy.resolve(selected)
            result = await registry.execute(call.name, call.arguments, scope=SimpleNamespace())
            policy.observe(call, result)
            return call, selected
        await step("arxiv")
        definition = policy.definitions()[0]
        assert "读取摘要" in definition.description and "获取此文献全文" not in definition.description
        _, stale = await step("Paper 1")
        with pytest.raises(ValueError, match="stale"):
            policy.resolve(stale)
        with pytest.raises(ValueError):
            policy.resolve(ModelToolCall(id="x", name="reader", arguments={"artifact_id": "abstract_1"}))
        assert len(policy.actions) == 2
        call, _ = await step("进一步获取")
        assert call.arguments["full_text_source_record_ids"] == ["src_1"]
        await step("读取已获取")
        call, _ = await step("下一段")
        assert call.arguments == {"artifact_id": "body_1", "char_start": 13, "max_chars": 8000}
        await step("arxiv")
        assert len(policy.actions) == 1
        assert "Paper 2" in policy.definitions()[0].description
        await step("Paper 2")
        await step("arxiv")
        assert not policy.definitions()  # Same discovery cannot resurrect consumed abstracts.
        assert [(c.artifact_id, c.char_start) for c in reader.calls] == [
            ("abstract_1", 0), ("body_1", 0), ("body_1", 13), ("abstract_2", 0)]
    asyncio.run(run())


def test_harness_rejects_raw_read_and_keeps_real_tool_trace_and_isolation():
    reader = Reader()
    registry = ResearcherToolRegistry([Database(), reader])
    class Model:
        def __init__(self):
            self.turn = 0
        async def acomplete(self, messages, *, options):
            self.turn += 1
            phase = (self.turn - 1) % 4
            if phase == 2:
                return ModelResponse(content=None, tool_calls=(ModelToolCall(id=str(self.turn), name="reader",
                    arguments={"artifact_id": "abstract_1"}),))
            if phase == 3:
                assert "select only option_id" in messages[-1].content
                return ModelResponse(content='{"cards":[],"no_evidence_reason":"test"}')
            definition = options.tools[0]
            option = definition.parameters["properties"]["option_id"]["enum"][0]
            return ModelResponse(content=None, tool_calls=(ModelToolCall(id=str(self.turn), name=definition.name,
                arguments={"option_id": option}),))
    harness = ToolCallHarness(Model(), registry, config=ToolCallHarnessConfig(max_turns=5),
                              action_policy_factory=ResearchReadingPolicy)
    async def run():
        for _ in range(2):
            result = await harness.run(system_prompt="test", initial_user_message="test", scope=SimpleNamespace())
            assert result.tool_calls_used == 2
            assert [e.tool_call.name for e in result.trace if e.kind == "tool_call"] == ["database_search", "reader"]
            assert sum(e.observation is not None for e in result.trace) == 2
    asyncio.run(run())
    assert len(reader.calls) == 2  # One per invocation; rejected raw calls never reach Reader.


def test_acquisition_without_body_returns_search_not_a_repeated_abstract():
    registry = ResearcherToolRegistry([Database(), Reader()])
    policy = ResearchReadingPolicy(registry, SimpleNamespace())
    call = ModelToolCall(id="a", name="database_search", arguments={
        "source_id": "arxiv", "full_text_source_record_ids": ["src_1"]})
    policy.observe(call, ResearcherToolObservation(tool_name="database_search", succeeded=True,
                   payload={"research_bundle": {"artifacts": [{"role": "abstract", "artifact_id": "abstract_1"}]}}))
    assert all(a.name == "database_search" and not a.arguments.get("full_text_source_record_ids")
               for a in policy.actions.values())


def test_fresh_workflow_import_has_no_validator_integrity_cycle():
    subprocess.run([sys.executable, "-c", "import novelty_agent_framework.workflows.research_task"], check=True)


def test_guided_prompt_exposes_only_selector_and_rejects_checkpoint_combo():
    from novelty_agent_framework.workflows.research_task import TaskResearcherConfig, TaskResearcherWorkflow
    registry = ResearcherToolRegistry([Database(), Reader()])
    workflow = TaskResearcherWorkflow(object(), registry, object(),
                                     config=TaskResearcherConfig(guided_reading=True))
    assert workflow._capability_note().startswith("Available tools: choose_research_action only.")
    with pytest.raises(ValueError, match="checkpoint"):
        TaskResearcherConfig(guided_reading=True, enable_evidence_checkpoint=True)


def test_read_recovery_uses_bound_artifacts_and_does_not_offer_unrelated_search():
    registry = ResearcherToolRegistry([Reader()])
    scope = SimpleNamespace(recovery=SimpleNamespace(
        action=SimpleNamespace(value="read_artifact"),
        artifacts=[SimpleNamespace(artifact_id="bound_body")]))
    policy = ResearchReadingPolicy(registry, scope)
    call = policy.resolve(choose(policy, "指定材料"))
    assert call.arguments["artifact_id"] == "bound_body"
    policy.observe(call, ResearcherToolObservation(tool_name="reader", succeeded=False, error="missing file"))
    assert not policy.definitions()


def test_text_choices_send_no_native_tools_and_preserve_reader_observations():
    class Model:
        def __init__(self): self.turn = 0
        async def acomplete(self, messages, *, options):
            self.turn += 1
            assert options.tools == () and options.tool_choice == "none"
            assert all(m.role != "tool" and not m.tool_calls for m in messages)
            if self.turn == 3:
                return ModelResponse(content='{"cards":[],"no_evidence_reason":"test"}')
            menu = json.loads(messages[-1].content.split("valid):\n", 1)[1].split("\nReturn", 1)[0])
            return ModelResponse(content=json.dumps({"option_id": next(iter(menu))}))
    reader = Reader()
    harness = ToolCallHarness(Model(), ResearcherToolRegistry([Database(), reader]),
        config=ToolCallHarnessConfig(text_choices=True, max_turns=4), action_policy_factory=ResearchReadingPolicy)
    result = asyncio.run(harness.run(system_prompt="test", initial_user_message="test", scope=SimpleNamespace()))
    assert result.tool_calls_used == 2
    assert len(reader.calls) == 1
    assert any(e.observation and e.tool_call.name == "reader" for e in result.trace)
    assert all(not e.message.tool_calls for e in result.trace if e.message)


def test_text_choices_cannot_execute_native_or_forged_actions_and_finalize_on_budget():
    class Model:
        def __init__(self): self.turn = 0
        async def acomplete(self, messages, *, options):
            self.turn += 1
            assert options.tools == () and options.tool_choice == "none"
            assert all(m.role != "tool" and not m.tool_calls for m in messages)
            if self.turn == 1:
                return ModelResponse(content=None, tool_calls=(ModelToolCall(id="x", name="reader", arguments={"artifact_id":"fake"}),))
            if self.turn == 2:
                return ModelResponse(content='{"option_id":"v1_0","artifact_id":"fake"}')
            return ModelResponse(content='{"cards":[],"no_evidence_reason":"budget"}')
    reader = Reader()
    harness = ToolCallHarness(Model(), ResearcherToolRegistry([Database(), reader]),
        config=ToolCallHarnessConfig(text_choices=True,max_turns=2,finalize_on_budget=True),
        action_policy_factory=ResearchReadingPolicy)
    result = asyncio.run(harness.run(system_prompt="test", initial_user_message="test", scope=SimpleNamespace()))
    assert not reader.calls and result.tool_calls_used == 0
    assert result.stop_reason
    assert len([e for e in result.trace if e.kind == "error" and "rejected selection" in e.detail]) == 2


def test_choice_only_prompt_omits_tool_catalog_and_native_skills():
    from novelty_agent_framework.workflows.research_task import TaskResearcherConfig, TaskResearcherWorkflow
    from test_evidence_card_builder import scope
    workflow = TaskResearcherWorkflow(object(), ResearcherToolRegistry([Database(),Reader()]), object(),
        config=TaskResearcherConfig(guided_reading=True,text_choices=True))
    system,user = workflow._render_prompt(scope())
    for forbidden in ("Available tools", "database_search", "reference_search", "web_search", "choose_research_action"):
        assert forbidden not in system + user
    assert "option_id" in system
