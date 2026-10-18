"""SearchPlanner Agent 的独立数据流和确定性校验测试（v2 契约）。

模型契约 v2：concepts（role/terms/alias/exclude/importance）+ strategies
（level/focus_concepts）；布尔表达式由 search_plan_compiler 模板生成。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.env import (ModelClientError, ModelProfile, ModelResponse,
                         OpenAICompatibleChatClient, PromptLibrary)
from novelty_agent_framework.core import RuntimeArtifactManager, RuntimeDebugConfig
from novelty_agent_framework.agents import (
    SearchPlannerAgent,
    SearchPlannerExhaustedError,
)
from novelty_agent_framework.schemas import NoveltyPoint, ResearchTask, SearchPlan
from novelty_agent_framework.agents.search_plan_compiler import (
    SearchPlanCompilationError, build_runtime_plan,
)
from novelty_agent_framework.schemas.search_plan_draft import SearchPlanDraft

PROMPTS_ROOT = Path("backend/src/novelty_agent_framework/prompts")


def repair_reply(draft):
    """Script a model explicitly selecting the current repair menu option."""
    def reply(options):
        schema = options.response_format["json_schema"]["schema"]
        option = schema["properties"]["option_id"]["enum"][0]
        return json.dumps({"option_id": option, "draft": draft})
    return reply


class StubModelClient:
    def __init__(self, *outputs: str) -> None:
        self.outputs = list(outputs)
        self.calls: list[tuple[list, object]] = []

    def complete(self, messages, *, options=None):  # type: ignore[no-untyped-def]
        self.calls.append((list(messages), options))
        index = min(len(self.calls) - 1, len(self.outputs) - 1)
        output = self.outputs[index]
        return ModelResponse(content=output(options) if callable(output) else output)


def make_point(*, english: bool = True) -> NoveltyPoint:
    return NoveltyPoint(
        point_id="NP-1",
        claim="采用动态邻居采样降低动态图神经网络训练通信开销",
        claim_en=(
            "Dynamic neighbor sampling reduces communication overhead in dynamic GNN training"
            if english
            else ""
        ),
        technical_features=["动态邻居采样", "分布式训练"],
    )


def make_task(*, language: str = "zh", task_type: str = "literature_search") -> ResearchTask:
    return ResearchTask(
        task_id="T-1" if language == "zh" else "T-2",
        novelty_point_id="NP-1",
        task_type=task_type,
        language=language,
        description=(
            "现有证据未覆盖动态邻居采样特征。"
            if task_type != "literature_search"
            else ""
        ),
    )


def valid_draft(task: ResearchTask, *, language: str | None = None) -> dict:
    """v2 最小模型契约：concepts（role/terms/...）+ strategies（level）。"""

    use_en = (language or task.language) == "en"
    if use_en:
        concepts = [
            {"role": "object", "terms": ["dynamic graph neural network"], "alias": ["DGNN"], "importance": 3},
            {"role": "method", "terms": ["dynamic neighbor sampling"], "importance": 3},
            {"role": "escape", "terms": ["communication efficient graph training"], "importance": 2},
        ]
    else:
        concepts = [
            {"role": "object", "terms": ["动态图神经网络"], "importance": 3},
            {"role": "method", "terms": ["动态邻居采样"], "importance": 3},
            {"role": "escape", "terms": ["低通信开销图训练"], "importance": 2},
        ]
    return {
        "concepts": concepts,
        "strategies": [{"level": "strict"}, {"level": "medium"}, {"level": "broad"}],
    }


def build_agent(client: StubModelClient) -> SearchPlannerAgent:
    return SearchPlannerAgent(
        model_client=client,
        prompts=PromptLibrary(PROMPTS_ROOT),
    )


def test_plans_normal_chinese_task_and_renders_prompt() -> None:
    task = make_task(language="zh")
    client = StubModelClient(json.dumps({"search_plan": valid_draft(task)}))

    plan = build_agent(client).plan(make_point(), task)

    assert isinstance(plan, SearchPlan)
    assert plan.task_id == "T-1"
    assert plan.novelty_point_id == "NP-1"
    assert plan.concepts[0].name == "动态图神经网络"  # name = terms[0]，补全器生成
    assert [c.concept_id for c in plan.concepts] == ["C1", "C2", "C3"]
    assert [c.role for c in plan.concepts] == ["object", "method", "escape"]
    assert [s.strategy_id for s in plan.strategies] == ["S1", "S2", "S3"]
    assert [strategy.level for strategy in plan.strategies] == [
        "strict",
        "medium",
        "broad",
    ]
    assert [s.use_alias for s in plan.strategies] == [False, True, True]
    assert plan.strategies[0].description == "动态图神经网络 AND 动态邻居采样"
    messages, options = client.calls[0]
    assert '"language": "zh"' in messages[1].content
    assert "SearchPlanDraft" in messages[0].content
    assert options.response_format == {"type": "json_object"}


@pytest.mark.parametrize("asynchronous", [False, True])
def test_planner_trace_links_model_json_draft_and_plan(tmp_path: Path, asynchronous: bool) -> None:
    task = make_task()
    client = OpenAICompatibleChatClient(ModelProfile(
        alias="planner", model="local", api_key="stub"))
    client._complete = lambda _messages, *, options=None: ModelResponse(
        content=json.dumps(valid_draft(task)))
    manager = RuntimeArtifactManager("paper", config=RuntimeDebugConfig(
        output_root=tmp_path / "outputs", archive_root=tmp_path / "archive"))
    manager.activate()
    stage = manager.start_stage("plan_research_task", {
        "current_point": make_point(), "current_task": task,
    })
    agent = SearchPlannerAgent(model_client=client)
    plan = asyncio.run(agent.aplan(make_point(), task)) if asynchronous else agent.plan(make_point(), task)
    manager.finish_stage(stage, {"search_plans": [plan]})
    manager.deactivate()
    events = [json.loads(path.read_text()) for path in sorted(
        (manager.run_dir / "planner_events").glob("*.json"))]
    assert [item["step"] for item in events] == [
        "parsed_model_json", "validated_draft", "compiled_plan"]
    assert events[0]["parent_llm_call_id"] == "llm_0001"
    assert events[1]["payload"]["concepts"][0]["role"] == "object"
    assert events[2]["payload"]["protected_concept_ids"] == ["C1"]


def test_planner_retry_trace_keeps_attempt_and_call_link(tmp_path: Path) -> None:
    task = make_task()
    responses = iter(["not json", repair_reply(valid_draft(task))])
    client = OpenAICompatibleChatClient(ModelProfile(
        alias="planner", model="local", api_key="stub"))
    def respond(_messages, *, options=None):
        output = next(responses)
        return ModelResponse(content=output(options) if callable(output) else output)
    client._complete = respond
    manager = RuntimeArtifactManager("paper", config=RuntimeDebugConfig(
        output_root=tmp_path / "outputs", archive_root=tmp_path / "archive"))
    manager.activate()
    stage = manager.start_stage("plan_research_task", {
        "current_point": make_point(), "current_task": task,
    })
    plan = SearchPlannerAgent(model_client=client, max_attempts=2).plan(make_point(), task)
    manager.finish_stage(stage, {"search_plans": [plan]})
    manager.deactivate()
    events = [json.loads(path.read_text()) for path in sorted(
        (manager.run_dir / "planner_events").glob("*.json"))]
    assert events[0]["attempt"] == 1 and events[0]["parent_llm_call_id"] == "llm_0001"
    assert events[-1]["attempt"] == 2 and events[-1]["parent_llm_call_id"] == "llm_0002"


def test_custom_prompt_name_is_used_for_rendering() -> None:
    task = make_task()
    client = StubModelClient(json.dumps(valid_draft(task)))

    class RecordingPrompts:
        def __init__(self) -> None:
            self.names: list[str] = []

        def render(self, name, **variables):
            self.names.append(name)
            return SimpleNamespace(system="system", user=json.dumps(variables))

    prompts = RecordingPrompts()
    agent = SearchPlannerAgent(
        model_client=client,
        prompts=prompts,
        prompt_name="test/custom_prompt",
    )

    agent.plan(make_point(), task)

    assert prompts.names == ["test/custom_prompt"]


def test_plans_english_task_when_point_has_no_english_claim() -> None:
    task = make_task(language="en")
    client = StubModelClient(json.dumps(valid_draft(task, language="en")))

    plan = build_agent(client).plan(make_point(english=False), task)

    assert plan.concepts[0].name == "dynamic graph neural network"
    assert len(client.calls) == 1


@pytest.mark.parametrize("where,exclude", [
    ("term", "streaming graph partitioning"),
    ("alias", "SGP"),
    ("core", "streaming"),
])
def test_exclude_conflicting_with_positive_concept_is_rejected(where, exclude):
    task = make_task(language="en")
    data = valid_draft(task)
    data["concepts"][0]["terms"] = ["streaming graph partitioning"]
    data["concepts"][0]["alias"] = ["SGP"]
    data["concepts"][1]["exclude"] = [exclude]
    with pytest.raises(SearchPlanCompilationError) as caught:
        build_runtime_plan(SearchPlanDraft.model_validate(data), task=task)
    assert any(issue.code == "exclude_conflicts_positive_concept"
               for issue in caught.value.issues)


def test_noise_exclude_does_not_conflict_with_positive_concepts():
    task = make_task(language="en")
    data = valid_draft(task)
    data["concepts"][0]["exclude"] = ["survey", "tutorial"]
    plan = build_runtime_plan(SearchPlanDraft.model_validate(data), task=task)
    assert plan.concepts[0].exclude == ["survey", "tutorial"]


@pytest.mark.parametrize("concepts,anchor", [
    ([
        {"role": "object", "terms": ["distributed graph neural network training"], "importance": 3},
        {"role": "method", "terms": ["graph summarization"], "importance": 3},
        {"role": "feature", "terms": ["mini-batch training"], "importance": 2},
    ], "C1"),
    ([
        {"role": "object", "terms": ["symbolic music generation"], "importance": 3},
        {"role": "method", "terms": ["hierarchical attention"], "importance": 3},
        {"role": "setting", "terms": ["long musical sequences"], "importance": 2},
    ], "C1"),
    ([
        {"role": "object", "terms": ["reaction"], "importance": 3},
        {"role": "method", "terms": ["catalytic bond cleavage"], "importance": 2},
        {"role": "feature", "terms": ["low temperature"], "importance": 1},
    ], "C2"),
])
def test_strict_medium_broad_preserve_same_anchor(concepts, anchor):
    task = make_task(language="en")
    draft = SearchPlanDraft.model_validate({
        "concepts": concepts,
        "strategies": [
            {"level": "strict", "focus_concepts": ["C1", "C2", "C3"]},
            {"level": "medium", "focus_concepts": ["C2", "C3"]},
            {"level": "broad", "focus_concepts": ["C3"]},
        ],
    })
    plan = build_runtime_plan(draft, task=task)
    expressions = {strategy.level: strategy.expression for strategy in plan.strategies}
    assert expressions["broad"] == anchor
    assert anchor in expressions["strict"].split(" AND ")
    assert anchor in expressions["medium"].split(" AND ")
    assert set(expressions["broad"].split(" AND ")) <= set(expressions["medium"].split(" AND "))
    assert set(expressions["medium"].split(" AND ")) <= set(expressions["strict"].split(" AND "))


def test_task_point_mismatch_fails_before_model_call() -> None:
    task = make_task()
    task = task.model_copy(update={"novelty_point_id": "NP-2"})
    client = StubModelClient(json.dumps(valid_draft(task)))

    with pytest.raises(ValueError, match="不一致"):
        build_agent(client).plan(make_point(), task)

    assert client.calls == []


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (
            lambda data: data["concepts"][0].update(terms=[""]),
            "词项全为空",
        ),
        (
            lambda data: data["concepts"][0].update(terms=["efficient robust learning"]),
            "generic_term",
        ),
    ],
)
def test_rejects_invalid_draft_semantics(mutation, message) -> None:  # type: ignore[no-untyped-def]
    task = make_task(language="en")
    data = valid_draft(task)
    mutation(data)
    output = json.dumps(data)
    client = StubModelClient(output, repair_reply(data))

    with pytest.raises(ValueError, match=message):
        build_agent(client).plan(make_point(), task)

    assert len(client.calls) == 2


def test_retries_once_after_invalid_json_then_succeeds() -> None:
    task = make_task()
    client = StubModelClient("not json", repair_reply(valid_draft(task)))

    plan = build_agent(client).plan(make_point(), task)

    assert plan.task_id == task.task_id
    assert len(client.calls) == 2
    assert "不是合法 JSON" in client.calls[1][0][1].content


def test_retries_after_model_network_error_then_succeeds() -> None:
    """网络重试保持原请求，不将网络原因当作草稿纠错反馈。"""

    task = make_task()

    class FlakyClient(StubModelClient):
        def __init__(self, *outputs: str) -> None:
            super().__init__(*outputs)
            self.fail_first = True

        def complete(self, messages, *, options=None):
            self.calls.append((list(messages), options))
            if self.fail_first:
                self.fail_first = False
                raise ModelClientError("模型网络调用失败: The read operation timed out")
            index = min(len(self.calls) - 1, len(self.outputs) - 1)
            return ModelResponse(content=self.outputs[index])

    client = FlakyClient(json.dumps(valid_draft(task)))
    plan = build_agent(client).plan(make_point(), task)

    assert plan.task_id == task.task_id
    assert len(client.calls) == 2
    assert client.calls[0][0] == client.calls[1][0]

def test_invalid_schema_fails_after_one_retry() -> None:
    client = StubModelClient("{}", repair_reply({}))

    with pytest.raises(SearchPlannerExhaustedError) as raised:
        build_agent(client).plan(make_point(), make_task())

    assert len(client.calls) == 2
    assert raised.value.audit["attempts"] == 2
    assert raised.value.audit["code"] == "model.output.schema_invalid"
    assert raised.value.audit["terminal_reason"] == "repair_budget_exhausted"
    assert "SearchPlanDraft schema" in raised.value.last_error


def test_legacy_v1_output_is_rejected() -> None:
    """v1 契约（含 expression）因 extra=forbid 被拒并重试。"""

    task = make_task()
    legacy = {
        "concepts": [{"terms": ["x"]}],
        "strategies": [{"expression": "C1"}],
    }
    client = StubModelClient(json.dumps(legacy), repair_reply(legacy))

    with pytest.raises(SearchPlannerExhaustedError):
        build_agent(client).plan(make_point(), task)

    assert len(client.calls) == 2


def test_structured_retry_feedback_is_forwarded_to_prompt() -> None:
    task = make_task(language="en")
    bad = valid_draft(task)
    bad["concepts"][0]["terms"] = ["efficient robust learning"]  # 泛词失败
    good = valid_draft(task)
    client = StubModelClient(json.dumps(bad), repair_reply(good))

    plan = build_agent(client).plan(make_point(), task)

    assert plan.strategies[0].expression == "C1 AND C2"
    retry_message = client.calls[1][0][1].content
    assert "generic_term" in retry_message
    assert '"code"' in retry_message


def test_supplement_task_can_use_focused_strategy_count() -> None:
    task = make_task(language="en", task_type="feature_supplement")
    data = valid_draft(task)
    data["strategies"] = [{"level": "strict", "focus_concepts": ["C2"]}]
    client = StubModelClient(json.dumps(data))

    plan = build_agent(client).plan(make_point(), task)

    assert len(plan.strategies) == 1
    assert plan.strategies[0].level == "strict"
    assert plan.strategies[0].expression == "C2"
    assert "动态邻居采样" in client.calls[0][0][1].content


class AsyncOnlyModelClient:
    def __init__(self, *outputs):
        self.outputs = list(outputs)
        self.calls = []

    def complete(self, messages, *, options=None):
        raise AssertionError("Async planning must not use synchronous complete")

    async def acomplete(self, messages, *, options=None):
        self.calls.append((list(messages), options))
        output = self.outputs.pop(0)
        if isinstance(output, Exception):
            raise output
        return ModelResponse(content=output(options) if callable(output) else output)


def test_async_planner_retries_with_feedback_and_keeps_task_binding():
    async def run():
        task = make_task()
        client = AsyncOnlyModelClient(
            ModelClientError("temporary failure"), "not json",
            repair_reply(valid_draft(task)),
        )
        agent = SearchPlannerAgent(model_client=client, prompts=PromptLibrary(PROMPTS_ROOT))
        plan = await agent.aplan(make_point(), task)
        assert (plan.novelty_point_id, plan.task_id) == (task.novelty_point_id, task.task_id)
        assert len(client.calls) == 3
        assert client.calls[0][0] == client.calls[1][0]
        assert "合法 JSON" in client.calls[2][0][1].content
    asyncio.run(run())


def test_async_planner_exhaustion_records_error_category():
    async def run():
        client = AsyncOnlyModelClient("not json", "still not json")
        with pytest.raises(SearchPlannerExhaustedError) as caught:
            await SearchPlannerAgent(model_client=client, max_attempts=2).aplan(make_point(), make_task())
        assert caught.value.audit["failure_category"] == "invalid_model_output"
        assert caught.value.attempts == 2
    asyncio.run(run())


def test_async_planner_requests_overlap_and_remain_isolated():
    async def run():
        entered = asyncio.Event()
        class Client(AsyncOnlyModelClient):
            async def acomplete(self, messages, *, options=None):
                self.calls.append((list(messages), options))
                if len(self.calls) == 2:
                    entered.set()
                await entered.wait()
                return ModelResponse(content=json.dumps(valid_draft(make_task())))
        client = Client()
        agent = SearchPlannerAgent(model_client=client, prompts=PromptLibrary(PROMPTS_ROOT))
        point1 = make_point()
        point2 = point1.model_copy(update={"point_id": "NP-2", "claim": "另一个独立机制"})
        task1 = make_task()
        task2 = task1.model_copy(update={"novelty_point_id": "NP-2"})
        plans = await asyncio.wait_for(asyncio.gather(
            agent.aplan(point1, task1), agent.aplan(point2, task2),
        ), timeout=3)
        assert {plan.novelty_point_id for plan in plans} == {"NP-1", "NP-2"}
        assert all(len(messages) == 2 for messages, _ in client.calls)
        assert '"point_id": "NP-2"' not in client.calls[0][0][1].content
        assert '"point_id": "NP-1"' not in client.calls[1][0][1].content
    asyncio.run(run())


def test_async_planner_cancellation_propagates_without_retry():
    async def run():
        entered = asyncio.Event()
        class Client(AsyncOnlyModelClient):
            async def acomplete(self, messages, *, options=None):
                self.calls.append((list(messages), options))
                entered.set()
                await asyncio.Event().wait()
        client = Client()
        agent = SearchPlannerAgent(model_client=client)
        pending = asyncio.create_task(agent.aplan(make_point(), make_task()))
        await asyncio.wait_for(entered.wait(), timeout=3)
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        assert len(client.calls) == 1
    asyncio.run(run())


def test_sync_planner_rejects_event_loop_use():
    async def run():
        with pytest.raises(RuntimeError, match="aplan"):
            build_agent(StubModelClient("unused")).plan(make_point(), make_task())
    asyncio.run(run())
