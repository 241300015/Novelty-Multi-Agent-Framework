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
        assert "读取摘要" in definition.description and "阅读此文献正文" not in definition.description
        _, stale = await step("Paper 1")
        with pytest.raises(ValueError, match="stale"):
            policy.resolve(stale)
        with pytest.raises(ValueError):
            policy.resolve(ModelToolCall(id="x", name="reader", arguments={"artifact_id": "abstract_1"}))
        assert len(policy.actions) == 2
        call, _ = await step("选择正文，留待")
        assert call.arguments["full_text_source_record_ids"] == ["src_1"]
        assert "读取已选文献全文" not in str(policy.choices())
        assert "Paper 2" in str(policy.choices())
        await step("Paper 2")
        call, _ = await step("继续处理本轮候选")
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
        prefer = ("选择正文，留待" if self.acquire else "继续处理本轮候选")
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
    assert workflow.harness.config.round_budgets
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
@pytest.mark.parametrize("round_budgets", [False, True])
def test_three_rounds_use_plan_subsets_and_never_reuse_previous_round_cache(failed, round_budgets):
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
            round_budgets=round_budgets,
            per_tool_limits={"database_search": 1 if round_budgets else 3}, reuse_database_results=True),
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


class RoundDatabase(Database):
    """Each strategy subset returns a different, fixed set of papers."""

    def __init__(self, papers=8):
        self.papers = papers
        self.discoveries = []

    async def ainvoke(self, arguments, *, scope):
        if arguments.full_text_source_record_ids:
            record = arguments.full_text_source_record_ids[0]
            return ResearcherToolObservation(tool_name=self.name, succeeded=True, payload={
                "research_bundle": {"artifacts": [{"artifact_id": f"body_{record}",
                    "source_record_id": record, "role": "extracted_text"}]}})
        round_id = 4 - len(scope.search_plan.strategies)
        self.discoveries.append(round_id)
        ids = [f"round{round_id}_paper{i}" for i in range(self.papers)]
        return ResearcherToolObservation(tool_name=self.name, succeeded=True, payload={
            "research_bundle": {
                "source_records": [{"source_record_id": rid, "source_id": "arxiv",
                                    "title": rid} for rid in ids],
                "artifacts": [{"artifact_id": f"abstract_{rid}", "source_record_id": rid,
                               "role": "abstract"} for rid in ids]}})


@pytest.mark.parametrize("projection", [False, True])
def test_each_round_can_read_eight_papers_and_budgets_reset_between_invocations(projection):
    db, reader = RoundDatabase(), Reader()

    class InspectModel(MenuModel):
        async def acomplete(self, messages, *, options):
            self.last_messages = messages
            return await super().acomplete(messages, options=options)

    model = InspectModel(stop=False)
    harness = ToolCallHarness(model, ResearcherToolRegistry([db, reader]),
        config=ToolCallHarnessConfig(text_choices=True, round_budgets=True,
            max_turns=18, max_tool_calls=9, per_tool_limits={"database_search": 1, "reader": 8},
            max_total_read_chars=104, report_budget=True, runtime_state_projection=projection,
            reuse_database_results=True),
        action_policy_factory=lambda registry, scope: ResearchReadingPolicy(registry, scope, max_chars=13))

    async def run():
        for _ in range(2):
            result = await harness.run(system_prompt="test", initial_user_message="test", scope=task_scope())
            assert result.tool_calls_used == 27  # Three searches plus 24 abstract reads.
            assert result.turns_used == 52 and not result.stop_reason
            assert not any(e.kind == "error" for e in result.trace)
            text = "\n".join(m.content or "" for m in model.last_messages)
            assert '"remaining_per_tool_by_round"' in text
            for index in range(1, 4):
                assert f'"{index}": {{"database_search": 0, "reader": 0}}' in text

    asyncio.run(run())
    assert db.discoveries == [1, 2, 3] * 2
    assert len(reader.calls) == 48
    assert harness.config.max_tool_calls == 9  # Shared configuration was not mutated.


@pytest.mark.parametrize("papers", [1, 7])
def test_deferred_body_reads_use_their_original_round_quotas(papers):
    db, reader = RoundDatabase(papers=papers), Reader()
    harness = ToolCallHarness(MenuModel(stop=False, acquire=True), ResearcherToolRegistry([db, reader]),
        config=ToolCallHarnessConfig(text_choices=True, round_budgets=True,
            max_turns=3 * papers + 2, max_tool_calls=3 * papers + 1,
            per_tool_limits={"database_search": papers + 1, "reader": 2 * papers}),
        action_policy_factory=ResearchReadingPolicy)
    result = asyncio.run(harness.run(system_prompt="test", initial_user_message="test", scope=task_scope()))
    assert result.tool_calls_used == 3 * (3 * papers + 1) and not result.stop_reason
    database_calls = [e for e in result.trace if e.kind == "tool_call" and e.tool_call.name == "database_search"]
    assert len(database_calls) == 3 * (papers + 1)  # An 8-call quota grants 24, not 8, overall.
    for index in (1, 2, 3):
        assert sum(e.detail == f"research_round:{index}" for e in database_calls) == papers + 1
    assert [c.artifact_id for c in reader.calls] == [
        *(f"abstract_round{i}_paper{p}" for i in (1, 2, 3) for p in range(papers)),
        *(f"body_round{i}_paper{p}" for i in (1, 2, 3) for p in range(papers))]


def test_one_round_cannot_borrow_unused_tool_quota_from_later_rounds():
    from novelty_agent_framework.core.tool_call_harness import ToolCallBudgetExhausted
    db, reader = RoundDatabase(papers=9), Reader()
    harness = ToolCallHarness(MenuModel(stop=False), ResearcherToolRegistry([db, reader]),
        config=ToolCallHarnessConfig(text_choices=True, round_budgets=True,
            max_turns=24, max_tool_calls=12, per_tool_limits={"database_search": 1, "reader": 8}),
        action_policy_factory=ResearchReadingPolicy)
    with pytest.raises(ToolCallBudgetExhausted, match="reader tool-call budget exhausted in research round 1"):
        asyncio.run(harness.run(system_prompt="test", initial_user_message="test", scope=task_scope()))
    assert len(reader.calls) == 8 and db.discoveries == [1]


@pytest.mark.parametrize("levels, count", [
    ((), 1), (("broad",), 1), (("medium", "broad"), 2), (("strict", "medium", "broad"), 3)])
def test_budget_scope_count_uses_only_nonempty_distinct_strategy_subsets(levels, count):
    policy = ResearchReadingPolicy(ResearcherToolRegistry([Database(), Reader()]), task_scope(levels))
    assert policy.budget_scope_count == count


@pytest.mark.parametrize("action", ["read_artifact", "fetch_fulltext", "retry_request", "change_provider",
                                  "replan_query", "research_gap"])
def test_recovery_keeps_one_round_budget_even_with_three_original_strategy_levels(action):
    request = task_scope().model_copy(update={"recovery": SimpleNamespace(
        action=SimpleNamespace(value=action), source_id="arxiv", source_record_ids=["record"],
        artifacts=[SimpleNamespace(artifact_id="body")])})
    policy = ResearchReadingPolicy(ResearcherToolRegistry([Database(), Reader()]), request)
    assert policy.budget_scope_count == 1
    for option in policy.choices():
        call = policy.resolve(policy.selection({"option_id": option}, "test"))
        if call is not None:
            assert policy.budget_scope(call) == 1
            break


@pytest.mark.parametrize("action", ["replan_query", "research_gap"])
def test_replanned_recovery_finishes_after_one_round_with_three_strategy_levels(action):
    request = task_scope().model_copy(update={"recovery": SimpleNamespace(
        action=SimpleNamespace(value=action), source_id="arxiv")})
    harness = ToolCallHarness(MenuModel(stop=False), ResearcherToolRegistry([Database(), Reader()]),
        config=ToolCallHarnessConfig(text_choices=True, round_budgets=True, max_turns=12),
        action_policy_factory=ResearchReadingPolicy)
    result = asyncio.run(harness.run(system_prompt="test", initial_user_message="test", scope=request))
    searches = [e for e in result.trace if e.kind == "tool_call" and e.tool_call.name == "database_search"]
    assert len(searches) == 1
    assert not result.stop_reason
    assert not any(e.kind == "error" for e in result.trace)


@pytest.mark.parametrize("failure_at", ["observe", "next_budget_scope"])
def test_policy_exception_preserves_completed_searches_in_partial_task(failure_at):
    from novelty_agent_framework.workflows import TaskResearcherConfig, TaskResearcherWorkflow
    from test_task_researcher_workflow import FakeBuilder

    class EmptyDatabase(Database):
        async def ainvoke(self, arguments, *, scope):
            return ResearcherToolObservation(tool_name=self.name, succeeded=True, payload={
                "search_executions": [{"execution_id": "completed-query", "tool_name": self.name,
                    "source_id": "arxiv", "query": "dynamic graph", "status": "succeeded",
                    "started_at": "2026-10-26T00:00:00+00:00", "results": []}]})

    class BrokenPolicy(ResearchReadingPolicy):
        def observe(self, call, observation):
            super().observe(call, observation)
            if failure_at == "observe":
                raise ValueError("injected policy transition failure")

        def budget_scope(self, call):
            if self.round_index:
                return 99
            return super().budget_scope(call)

    workflow = TaskResearcherWorkflow(MenuModel(stop=False),
        ResearcherToolRegistry([EmptyDatabase(), Reader()]), FakeBuilder(),
        config=TaskResearcherConfig(guided_reading=True, text_choices=True))
    workflow.harness.action_policy_factory = BrokenPolicy
    result = asyncio.run(workflow.ainvoke(task_scope()))
    assert result.status.value == "partial"
    assert [e.execution_id for e in result.search_executions] == ["completed-query"]
    assert result.search_executions[0].status.value == "succeeded"
    assert result.execution_failures
    assert result.evidence_cards == []


def test_policy_cancellation_is_not_converted_into_partial_result():
    class CancelledPolicy(ResearchReadingPolicy):
        def observe(self, call, observation):
            raise asyncio.CancelledError()

    harness = ToolCallHarness(MenuModel(), ResearcherToolRegistry([Database(), Reader()]),
        config=ToolCallHarnessConfig(text_choices=True), action_policy_factory=CancelledPolicy)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(harness.run(system_prompt="test", initial_user_message="test", scope=task_scope()))


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
        assert policy.resolve(choose(policy, "继续处理本轮候选")) is None
    assert policy.can_finish and not policy._searches()
    with pytest.raises(ValueError, match="no remaining"):
        policy.execution_scope(ModelToolCall(id="forged", name="database_search", arguments={"source_id":"arxiv"}))


def test_guided_menu_reads_record_abstract_and_acquired_raw_full_text(tmp_path):
    from novelty_agent_framework.schemas import TaskResearchRequest
    from novelty_agent_framework.tools import ReaderTool, ReferenceArtifactReaderTool
    from novelty_agent_framework.tools.database_search import DatabaseSearchTool
    from test_arxiv_materials import acquisition_tool

    retrieval, request = acquisition_tool(tmp_path)
    store = retrieval.reference_store
    registry = ResearcherToolRegistry([DatabaseSearchTool({"arxiv": retrieval}, store),
                                       ReaderTool(ReferenceArtifactReaderTool(store))])
    scope = TaskResearchRequest(**request.model_dump(exclude={"source_id"}))
    policy = ResearchReadingPolicy(registry, scope)

    async def select(label):
        call = policy.resolve(choose(policy, label))
        obs = await registry.execute(call.name, call.arguments, scope=policy.execution_scope(call))
        assert obs.succeeded, obs.error
        policy.observe(call, obs)
        return obs

    async def run():
        await select("执行第")
        abstract = await select("读取摘要")
        assert abstract.payload["read_result"]["text"] == "A search abstract."
        acquisition = await select("阅读此文献正文")
        raw, = acquisition.payload["research_bundle"]["artifacts"]
        assert raw["role"] == "full_text"
        body = await select("读取已获取的候选全文")
        assert "Method uses a graph." in body.payload["read_result"]["text"]
        assert body.payload["candidate_page"]["handle"]["raw_id"] == raw["artifact_id"]
        for obs in (abstract, body):
            page = obs.payload["candidate_page"]
            texts = []
            for citation in page["citations"]:
                replay = await registry.execute("reader", {"request": {"operation":"read",
                    "handle":page["handle"], "citation":citation}}, scope=scope)
                assert replay.succeeded
                texts.append(replay.payload["record_result"]["blocks"][0]["content"])
            visible = registry.project_model_context("reader", obs)
            assert "".join(texts) == visible["read_result"]["text"]
            assert "handle" not in visible and "candidate_page" not in visible

    asyncio.run(run())


@pytest.mark.parametrize("body_format", ["html", "pdf", "unavailable"])
def test_card_from_abstract_triggers_numbered_acquisition_without_model_turn(tmp_path, body_format):
    import httpx
    from novelty_agent_framework.core import RuntimeArtifactManager
    from novelty_agent_framework.schemas import TaskResearchRequest
    from novelty_agent_framework.tools import EvidenceCardBuilder, ReaderTool, ReferenceArtifactReaderTool
    from novelty_agent_framework.tools.database_search import DatabaseSearchTool
    from novelty_agent_framework.workflows import TaskResearcherConfig, TaskResearcherWorkflow
    from test_arxiv_materials import HTML, acquisition_tool, html_response, pdf_bytes
    from test_runtime_artifacts import _config

    available = body_format != "unavailable"
    original = pdf_bytes() if body_format == "pdf" else HTML
    requests = []

    def serve(req):
        requests.append(str(req.url))
        if body_format == "pdf":
            return httpx.Response(404) if "/html/" in req.url.path else httpx.Response(
                200, content=original, headers={"content-type": "application/pdf"})
        return html_response(req) if available else httpx.Response(403)

    retrieval, discovery = acquisition_tool(tmp_path, handler=serve)
    scope = TaskResearchRequest(**discovery.model_dump(exclude={"source_id"}))
    store = retrieval.reference_store
    tools = ResearcherToolRegistry([DatabaseSearchTool({"arxiv": retrieval}, store),
                                    ReaderTool(ReferenceArtifactReaderTool(store))])
    class CardModel(MenuModel):
        turns = 0

        async def acomplete(self, messages, *, options):
            self.turns += 1
            response = await super().acomplete(messages, options=options)
            if "cards" in response.content:
                assert not requests  # The model explicitly moved on after the abstract.
                return ModelResponse(content=json.dumps({"cards": [{
                    "main_contribution": "Selected from abstract", "confidence": 0.8, "relevance": 0.8,
                    "quotes": [{"quote": "A search abstract.", "interpretation": "Observed", "confidence": 0.8}]
                }]}))
            return response

    model = CardModel()
    workflow = TaskResearcherWorkflow(model, tools, EvidenceCardBuilder(store),
        config=TaskResearcherConfig(guided_reading=True, text_choices=True))
    runtime = RuntimeArtifactManager(scope.subject_paper_id, config=_config(tmp_path))
    runtime.activate()
    try:
        result = asyncio.run(workflow.ainvoke(scope))
    finally:
        runtime.deactivate()

    card, = result.evidence_cards
    item, = result.evidence
    record = item.provenance["source_record_id"]
    assert result.status == "completed" and result.steps_used == model.turns
    assert [r.role.value for r in result.read_results] == ["abstract"]
    expected = ["https://arxiv.org/html/2402.03300v1"]
    if body_format == "pdf":
        expected.append("https://arxiv.org/pdf/2402.03300v1")
    assert requests == expected
    logs = [json.loads(p.read_text()) for p in (runtime.run_dir / "tools").glob("*_database_search.json")]
    auto, = [log for log in logs if log["resolved_arguments"].get("full_text_source_record_ids")]
    assert auto["tool_call_id"] and auto["parent_llm_call_id"] is None and auto["agent_arguments"] is None
    assert auto["normalized_result"]["card_ids_by_record"] == {record: [card.card_id]}
    ids = auto["normalized_result"]["full_text_artifact_ids"][record]
    assert auto["business_status"] == ("ACQUIRED" if available else "UNAVAILABLE")
    assert bool(ids) == available
    assert ids == [a.artifact_id for a in result.research_bundles[-1].artifacts]
    assert set(ids) <= set(result.candidate_audit[0].artifact_ids)

    if available:
        import hashlib
        raw, path, data = store.verify_artifact_file(scope.subject_paper_id, ids[0])
        assert data == original and raw.sha256 == hashlib.sha256(data).hexdigest()
        assert raw.byte_size == len(data) and raw.source_record_id == record
        _verify_reviewer_checkpoint(tmp_path, scope, result, store, raw, body_format)
        assert requests == expected  # All review and recovery operations were offline.


def _verify_reviewer_checkpoint(tmp_path, scope, result, store, raw, body_format):
    from novelty_agent_framework.agents import NoveltyEvidenceReviewer
    from novelty_agent_framework.schemas import NoveltyPointReviewRequest
    from novelty_agent_framework.tools import ReviewerReaderTool, ReferenceArtifactReaderTool
    from test_reviewer_summary_checkpoint import Client, output

    request = NoveltyPointReviewRequest(subject_paper_id=scope.subject_paper_id,
        novelty_point=scope.novelty_point, tasks=[scope.research_task],
        cards=result.evidence_cards, evidence=result.evidence)
    tool = ReviewerReaderTool(ReferenceArtifactReaderTool(store))
    catalog = tool.material_catalog(request)
    body, = [e for e in catalog if e["role"] == "extracted_text"]
    assert raw.artifact_id not in json.dumps(catalog)
    assert store.find_artifact(scope.subject_paper_id, body["artifact_id"]) is None
    assert not list(tmp_path.rglob("*.map.json"))  # Listing must not parse the original.

    class ReviewClient(Client):
        async def acomplete(self, messages, *, options=None):
            if self.responses:
                return await super().acomplete(messages, options=options)
            if messages[-1].role != "tool":
                return ModelResponse(content=None, tool_calls=(ModelToolCall(id="body", name="reader",
                    arguments={"artifact_id": body["artifact_id"], "char_start": 2, "max_chars": 400}),))
            read = json.loads(messages[-1].content)["read_result"]
            quote = "Method uses a graph." if body_format == "html" else "PDF document text."
            start = read["char_start"] + read["text"].index(quote)
            draft = json.loads(output(request).content)
            draft["read_citations"] = [{"read_id":read["read_id"], "char_start":start, "char_end":start+len(quote)}]
            return ModelResponse(content=json.dumps(draft))

    client = ReviewClient()
    reviewer = NoveltyEvidenceReviewer(client, tool_registry=ResearcherToolRegistry([tool]))
    review = asyncio.run(reviewer.review(request))
    addition, = review.review_evidence
    assert addition.exact_quote == ("Method uses a graph." if body_format == "html" else "PDF document text.")
    assert addition.artifact_id == body["artifact_id"]
    assert store.find_artifact(scope.subject_paper_id, addition.artifact_id).derived_from_artifact_id == raw.artifact_id
    audit, = review.reader_observations
    replay = store.read_document_slice(scope.subject_paper_id, artifact_id=audit.artifact_id,
        char_start=audit.char_start, max_chars=audit.char_end-audit.char_start)
    assert audit.read_id == addition.read_id == replay.read_id
    assert result.evidence[0].provenance["read_id"] == store.read_document_slice(scope.subject_paper_id,
        artifact_id=result.evidence[0].artifact_id, char_start=0, max_chars=8000).read_id

    rows = [{"index":1, "card_id":request.cards[0].card_id,"novelty_point_id":request.novelty_point.point_id,
             "status":"completed", "review":review.model_dump(mode="json")}]
    client.responses = [TimeoutError("inject summary timeout"), output(request, reviewed=True)]
    initial = asyncio.run(reviewer.summarize_with_checkpoint(request, rows, output_root=tmp_path, run_id="roundtrip"))
    assert initial.status == "failed" and initial.durable and initial.checkpoint_id
    restored = asyncio.run(reviewer.recover_summary(request, output_root=tmp_path,
        run_id="roundtrip", checkpoint_id=initial.checkpoint_id))
    assert restored.status == "completed" and restored.partial_card_results == rows
    assert restored.review.review_evidence == [addition]

    if body_format == "html":
        from novelty_agent_framework.schemas.materials import MaterialScope
        from novelty_agent_framework.tools.full_text_reader import FullTextReader
        from test_arxiv_materials import acquisition_tool

        other_retrieval, other_request = acquisition_tool(tmp_path, versions=("v2",), full_text_limit=1)
        other_bundle = asyncio.run(other_retrieval.ainvoke(other_request))
        other_record, = other_bundle.source_records
        other_raw, = other_bundle.artifacts
        assert other_record.work_id == raw.work_id and other_raw.version_label == "v2"
        material_scope = MaterialScope(subject_paper_id=scope.subject_paper_id,
                                       namespace="research_reference")
        other_id = FullTextReader(store).text_selections(
            material_scope, store.load_manifest(scope.subject_paper_id),
            {other_record.source_record_id},
        )[other_raw.artifact_id]
        assert other_id not in {row["artifact_id"] for row in tool.material_catalog(request)}
        evidence = request.evidence[0]
        other_provenance = {**evidence.provenance, "source_record_id": other_record.source_record_id}
        wrong_version = request.model_copy(update={
            "evidence": [evidence.model_copy(update={"provenance": other_provenance})],
        })
        # The persisted citation pins the record even when the ancillary metadata disagrees.
        with pytest.raises(PermissionError, match="outside reviewer scope"):
            asyncio.run(tool.ainvoke(tool.args_schema(artifact_id=other_id), scope=wrong_version))


def test_card_fetch_batches_distinct_records_and_preserves_cards_on_catalog_error(tmp_path):
    from novelty_agent_framework.core import RuntimeArtifactManager
    from novelty_agent_framework.workflows.card_full_text import acquire_card_full_texts
    from test_runtime_artifacts import _config

    class DatabaseWithPartialResults(Database):
        def __init__(self):
            self.reference_store = self
            self.batches = []

        def load_manifest(self, paper_id):
            raise OSError("catalog unavailable")

        async def ainvoke(self, arguments, *, scope):
            ids = arguments.full_text_source_record_ids
            self.batches.append(ids)
            return ResearcherToolObservation(tool_name=self.name, succeeded=True, payload={
                "database_search_result": {"results": [
                    {"source_record_id": rid, "full_text_artifact_ids": ["file"] if i == 0 else []}
                    for i, rid in enumerate(ids)]}})

    db = DatabaseWithPartialResults()
    registry = ResearcherToolRegistry([db])
    records = [SimpleNamespace(source_record_id=str(i), source_id="arxiv") for i in range(5)]
    ids = ["missing", "0", "1", "2", "3", "4", "0"]
    evidence = [SimpleNamespace(evidence_id=f"e{i}", provenance={
        "artifact_namespace": "research_reference", "source_kind": "structured_database",
        "source_record_id": rid}) for i, rid in enumerate(ids)]
    cards = [SimpleNamespace(card_id=f"c{i}", evidence_ids=[e.evidence_id]) for i, e in enumerate(evidence)]
    result = SimpleNamespace(research_bundles=[SimpleNamespace(source_records=records, artifacts=[])],
                             evidence=evidence, evidence_cards=cards)
    runtime = RuntimeArtifactManager("paper", config=_config(tmp_path))
    runtime.activate()
    try:
        events, warnings = asyncio.run(acquire_card_full_texts(registry, task_scope(), result))
    finally:
        runtime.deactivate()
    assert db.batches == [["0", "1", "2", "3"], ["4"]]
    assert len(events) == 2 and any("catalog unavailable" in w for w in warnings)
    first = json.loads((runtime.run_dir / "tools/0001_database_search.json").read_text())
    assert first["business_status"] == "PARTIAL"
    assert first["normalized_result"]["card_ids_by_record"]["0"] == ["c1", "c6"]


def test_original_provider_acquires_raw_when_card_already_has_legacy_text(tmp_path):
    from novelty_agent_framework.schemas import ArtifactRole, ContentExtent, TaskResearchRequest
    from novelty_agent_framework.tools.database_search import DatabaseSearchTool
    from novelty_agent_framework.workflows.card_full_text import acquire_card_full_texts
    from test_arxiv_materials import HTML, acquisition_tool, html_response

    requests = []

    def serve(req):
        requests.append(req.url.path)
        return html_response(req)

    retrieval, discovery = acquisition_tool(tmp_path, handler=serve)
    store = retrieval.reference_store
    scope = TaskResearchRequest(**discovery.model_dump(exclude={"source_id"}))
    bundle = asyncio.run(retrieval.ainvoke(discovery))
    record, = bundle.source_records
    historical = retrieval._try_save_artifact(
        scope.subject_paper_id, work=bundle.works[0], record=record, role=ArtifactRole.EXTRACTED_TEXT,
        content="Historical extract.", extent=ContentExtent.FULL, provenance={}, warnings=[],
    )
    store.merge_manifest(scope.subject_paper_id, artifacts=[historical])
    result = SimpleNamespace(
        research_bundles=[bundle.model_copy(update={"artifacts": [historical]})],
        evidence=[SimpleNamespace(evidence_id="e1", provenance={"artifact_namespace": "research_reference",
                  "source_kind": "structured_database", "source_record_id": record.source_record_id})],
        evidence_cards=[SimpleNamespace(card_id="c1", evidence_ids=["e1"])],
    )
    registry = ResearcherToolRegistry([DatabaseSearchTool({"arxiv": retrieval}, store)])
    events, warnings = asyncio.run(acquire_card_full_texts(registry, scope, result))
    event, = events
    assert not warnings and requests == ["/html/2402.03300v1"]
    artifact, = event.observation.payload["research_bundle"]["artifacts"]
    raw, _, body = store.verify_artifact_file(scope.subject_paper_id, artifact["artifact_id"])
    assert raw.source_record_id == record.source_record_id and body == HTML
    assert store.verify_artifact_file(scope.subject_paper_id, historical.artifact_id)[2] == b"Historical extract."


def test_interrupted_discovery_distinguishes_unread_json_abstract_from_no_material():
    from novelty_agent_framework.core.tool_call_harness import ToolCallHarnessEvent
    from novelty_agent_framework.workflows.candidate_audit import build_candidate_audit

    records = [
        {"source_record_id": "with", "work_id": "w1", "abstract": "Available in catalog JSON"},
        {"source_record_id": "without", "work_id": "w2", "abstract": None},
    ]
    observation = ResearcherToolObservation(tool_name="database_search", succeeded=True,
        payload={"research_bundle": {"source_records": records, "artifacts": []}})
    trace = [ToolCallHarnessEvent(kind="tool_result", observation=observation)]
    audit = build_candidate_audit(trace, [], [], [], interrupted=True)
    assert {item.source_record_id: item.status for item in audit} == {
        "with": "not_read", "without": "acquisition_unavailable",
    }
