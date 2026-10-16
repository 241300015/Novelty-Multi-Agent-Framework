"""Guided selection must constrain actual I/O, not merely advise the model."""
import asyncio
import json
import subprocess
import sys
from types import SimpleNamespace

import pytest

from backend.env import ModelResponse, ModelToolCall
from novelty_agent_framework.core.tool_call_harness import ToolCallHarness, ToolCallHarnessConfig
from novelty_agent_framework.schemas import DatabaseSearchArguments, ResearcherToolObservation, SearchStrategy
from novelty_agent_framework.schemas.research_tools import ReaderCallArguments
from novelty_agent_framework.tools import ResearcherToolRegistry
from novelty_agent_framework.workflows.research_reading_policy import ResearchReadingPolicy


def task_scope(levels=("strict", "medium", "broad")):
    from test_evidence_card_builder import scope
    request = scope()
    expression = request.search_plan.strategies[0].expression
    plan = request.search_plan.model_copy(update={"strategies": [
        SearchStrategy(strategy_id=f"S{i}", level=level, expression=expression)
        for i, level in enumerate(levels)]})
    return request.model_copy(update={"search_plan": plan})


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
        policy = ResearchReadingPolicy(registry, task_scope())
        async def step(label):
            selected = choose(policy, label)
            call = policy.resolve(selected)
            if call is not None:
                result = await registry.execute(call.name, call.arguments, scope=policy.execution_scope(call))
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
        call, _ = await step("获取此文献全文，留待")
        assert call.arguments["full_text_source_record_ids"] == ["src_1"]
        assert "读取已选文献全文" not in str(policy.choices())
        assert "Paper 2" in str(policy.choices())
        await step("Paper 2")
        call, _ = await step("不获取")
        assert call is None  # A candidate decision has no tool call or budget charge.
        assert "第 2 轮" in str(policy.choices())
        await step("停止检索")
        await step("读取已选")
        call, _ = await step("下一段")
        assert call.arguments == {"artifact_id": "body_1", "char_start": 13, "max_chars": 8000}
        assert policy.can_finish and not policy.definitions()
        assert [(c.artifact_id, c.char_start) for c in reader.calls] == [
            ("abstract_1", 0), ("abstract_2", 0), ("body_1", 0), ("body_1", 13)]
    asyncio.run(run())


class MenuModel:
    """Select by intent, keeping the model-facing protocol observable."""
    def __init__(self, *, native=False, reject_once=False, acquire=False, stop=True):
        self.native, self.reject_once, self.acquire, self.stop = native, reject_once, acquire, stop
        self.sent_invalid = False

    async def acomplete(self, messages, *, options):
        if self.native:
            menu = (json.loads(options.tools[0].description.split("选项：", 1)[1])
                    if options.tools else {})
        else:
            assert options.tools == () and options.tool_choice == "none"
            assert all(m.role != "tool" and not m.tool_calls for m in messages)
            menu = json.loads(messages[-1].content.split("valid):\n", 1)[1].split("\nReturn", 1)[0])
        if self.reject_once and not self.sent_invalid:
            self.sent_invalid = True
            return ModelResponse(content=None, tool_calls=(ModelToolCall(id="bad", name="reader",
                arguments={"artifact_id": "abstract_1"}),))
        if not menu:
            return ModelResponse(content='{"cards":[],"no_evidence_reason":"test"}')
        # At the initial boundary search; after reading abstracts optionally stop.
        prefer = ("获取此文献全文，留待" if self.acquire else "不获取")
        if self.stop and any("第 2 轮" in label for label in menu.values()):
            prefer = "停止检索"
        key = next((k for k, v in menu.items() if prefer in v), next(iter(menu)))
        if self.native:
            return ModelResponse(content=None, tool_calls=(ModelToolCall(id=key,
                name="choose_research_action", arguments={"option_id": key}),))
        return ModelResponse(content=json.dumps({"option_id": key}))


def test_harness_rejects_raw_read_and_keeps_real_tool_trace_and_isolation():
    reader = Reader()
    registry = ResearcherToolRegistry([Database(), reader])
    harness = ToolCallHarness(MenuModel(native=True, reject_once=True), registry,
        config=ToolCallHarnessConfig(max_turns=12), action_policy_factory=ResearchReadingPolicy)
    async def run():
        for _ in range(2):
            result = await harness.run(system_prompt="test", initial_user_message="test", scope=task_scope())
            assert result.tool_calls_used == 3
            assert [e.tool_call.name for e in result.trace if e.kind == "tool_call"] == ["database_search", "reader", "reader"]
            assert sum(e.observation is not None for e in result.trace) == 3
    asyncio.run(run())
    assert len(reader.calls) == 4


def test_acquisition_without_body_returns_search_not_a_repeated_abstract():
    registry = ResearcherToolRegistry([Database(), Reader()])
    policy = ResearchReadingPolicy(registry, task_scope())
    call = ModelToolCall(id="a", name="database_search", arguments={
        "source_id": "arxiv", "full_text_source_record_ids": ["src_1"]})
    policy.observe(call, ResearcherToolObservation(tool_name="database_search", succeeded=True,
                   payload={"research_bundle": {"artifacts": [{"role": "abstract", "artifact_id": "abstract_1"}]}}))
    assert not policy.bodies
    assert any(a.name == "database_search" for a in policy.actions.values())
    assert all(a.name != "reader" for a in policy.actions.values())


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
    reader = Reader()
    harness = ToolCallHarness(MenuModel(), ResearcherToolRegistry([Database(), reader]),
        config=ToolCallHarnessConfig(text_choices=True, max_turns=12), action_policy_factory=ResearchReadingPolicy)
    result = asyncio.run(harness.run(system_prompt="test", initial_user_message="test", scope=task_scope()))
    assert result.tool_calls_used == 3
    assert len(reader.calls) == 2
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
    result = asyncio.run(harness.run(system_prompt="test", initial_user_message="test", scope=task_scope()))
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


@pytest.mark.parametrize("failed", [False, True])
def test_three_rounds_use_plan_subsets_and_never_reuse_previous_round_cache(failed):
    class EmptyDatabase(Database):
        def __init__(self): self.plans = []
        async def ainvoke(self, arguments, *, scope):
            self.plans.append(scope.search_plan.model_dump(mode="json"))
            return ResearcherToolObservation(tool_name=self.name, succeeded=not failed,
                error="provider failed" if failed else None,
                payload={"research_bundle": {"source_records": [], "artifacts": []}})
    db = EmptyDatabase()
    request = task_scope()
    original = request.model_dump(mode="json")
    harness = ToolCallHarness(MenuModel(stop=False), ResearcherToolRegistry([db, Reader()]),
        config=ToolCallHarnessConfig(text_choices=True, max_turns=5, max_tool_calls=3,
            per_tool_limits={"database_search": 3}, reuse_database_results=True),
        action_policy_factory=ResearchReadingPolicy)
    result = asyncio.run(harness.run(system_prompt="test", initial_user_message="test", scope=request))
    assert [[s["level"] for s in p["strategies"]] for p in db.plans] == [
        ["strict", "medium", "broad"], ["medium", "broad"], ["broad"]]
    assert result.tool_calls_used == 3 and not result.stop_reason
    assert request.model_dump(mode="json") == original
    for plan in db.plans:
        for strategy in plan["strategies"]:
            assert strategy in original["search_plan"]["strategies"]


def test_repeated_candidates_are_not_reread_and_three_rounds_still_stop():
    class CountingDatabase(Database):
        def __init__(self): self.calls = 0
        async def ainvoke(self, arguments, *, scope):
            self.calls += 1
            return await super().ainvoke(arguments, scope=scope)
    db, reader = CountingDatabase(), Reader()
    harness = ToolCallHarness(MenuModel(stop=False), ResearcherToolRegistry([db, reader]),
        config=ToolCallHarnessConfig(text_choices=True, max_turns=12, reuse_database_results=True),
        action_policy_factory=ResearchReadingPolicy)
    result = asyncio.run(harness.run(system_prompt="test", initial_user_message="test", scope=task_scope()))
    assert db.calls == 3 and len(reader.calls) == 2
    assert result.tool_calls_used == 5 and not result.stop_reason


@pytest.mark.parametrize("levels, rounds", [((), 0), (("broad",), 1), (("medium", "broad"), 2)])
def test_missing_or_empty_strategy_sets_never_create_empty_search_calls(levels, rounds):
    class EmptyDatabase(Database):
        def __init__(self): self.calls = 0
        async def ainvoke(self, arguments, *, scope):
            assert scope.search_plan.strategies
            self.calls += 1
            return ResearcherToolObservation(tool_name=self.name, succeeded=True, payload={})
    db = EmptyDatabase()
    harness = ToolCallHarness(MenuModel(stop=False), ResearcherToolRegistry([db, Reader()]),
        config=ToolCallHarnessConfig(text_choices=True, max_turns=5), action_policy_factory=ResearchReadingPolicy)
    result = asyncio.run(harness.run(system_prompt="test", initial_user_message="test", scope=task_scope(levels)))
    assert db.calls == rounds and result.tool_calls_used == rounds


def test_selected_bodies_read_only_after_all_abstracts_and_stop_without_new_discovery():
    reader = Reader()
    harness = ToolCallHarness(MenuModel(acquire=True), ResearcherToolRegistry([Database(), reader]),
        config=ToolCallHarnessConfig(text_choices=True, max_turns=14), action_policy_factory=ResearchReadingPolicy)
    result = asyncio.run(harness.run(system_prompt="test", initial_user_message="test", scope=task_scope()))
    assert [(c.artifact_id, c.char_start) for c in reader.calls] == [
        ("abstract_1", 0), ("abstract_2", 0), ("body_1", 0), ("body_1", 13)]
    calls = [e.tool_call for e in result.trace if e.kind == "tool_call"]
    assert len([c for c in calls if c.name == "database_search" and not c.arguments.get("full_text_source_record_ids")]) == 1
    assert not result.stop_reason


def test_early_finish_cannot_skip_the_stop_and_fulltext_stages():
    class EarlyModel(MenuModel):
        async def acomplete(self, messages, *, options):
            if not self.sent_invalid:
                self.sent_invalid = True
                return ModelResponse(content='{"cards":[],"no_evidence_reason":"early"}')
            return await super().acomplete(messages, options=options)
    harness = ToolCallHarness(EarlyModel(), ResearcherToolRegistry([Database(), Reader()]),
        config=ToolCallHarnessConfig(text_choices=True, max_turns=14), action_policy_factory=ResearchReadingPolicy)
    result = asyncio.run(harness.run(system_prompt="test", initial_user_message="test", scope=task_scope()))
    assert any("rejected early finish" in (e.detail or "") for e in result.trace)
    assert result.tool_calls_used == 3


def test_eight_abstracts_need_one_discovery_and_local_switches_only():
    class EightDatabase(Database):
        async def ainvoke(self, arguments, *, scope):
            records = [{"source_record_id": f"src_{i}", "work_id": f"wrk_{i}",
                        "source_id": "arxiv", "title": f"Paper {i}"} for i in range(8)]
            artifacts = [{"artifact_id": f"abstract_{i}", "source_record_id": f"src_{i}",
                          "role": "abstract"} for i in range(8)]
            return ResearcherToolObservation(tool_name=self.name, succeeded=True,
                payload={"research_bundle": {"source_records": records, "artifacts": artifacts}})
    reader = Reader()
    harness = ToolCallHarness(MenuModel(), ResearcherToolRegistry([EightDatabase(), reader]),
        config=ToolCallHarnessConfig(text_choices=True, max_turns=22, max_tool_calls=9,
            per_tool_limits={"database_search": 1}), action_policy_factory=ResearchReadingPolicy)
    result = asyncio.run(harness.run(system_prompt="test", initial_user_message="test", scope=task_scope()))
    assert len(reader.calls) == 8 and result.tool_calls_used == 9
    assert len({c.artifact_id for c in reader.calls}) == 8
    assert not result.stop_reason


@pytest.mark.parametrize("action", ["retry_request", "change_provider"])
def test_discovery_recovery_preserves_frozen_scope_and_cannot_acquire_unrelated_bodies(action):
    request = task_scope().model_copy(update={"recovery": SimpleNamespace(
        action=SimpleNamespace(value=action), source_id="arxiv")})
    registry = ResearcherToolRegistry([Database(), Reader()])
    policy = ResearchReadingPolicy(registry, request)
    call = policy.resolve(choose(policy, "arxiv"))
    assert policy.execution_scope(call) is request
    observation = asyncio.run(registry.execute(call.name, call.arguments, scope=request))
    policy.observe(call, observation)
    for label in ("Paper 1", "Paper 2"):
        call = policy.resolve(choose(policy, label))
        policy.observe(call, asyncio.run(registry.execute(call.name, call.arguments, scope=request)))
        assert all(a.name == "_skip" for a in policy.actions.values())
        assert policy.resolve(choose(policy, "不获取")) is None
    assert policy.can_finish and not policy._searches()
    with pytest.raises(ValueError, match="no remaining"):
        policy.execution_scope(ModelToolCall(id="forged", name="database_search", arguments={"source_id":"arxiv"}))
