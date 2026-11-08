import json

import pytest

from test_search_planner import (StubModelClient, make_point, make_task, valid_draft,
                                 SearchPlannerAgent, SearchPlannerExhaustedError)


@pytest.fixture(autouse=True)
def isolated_planner_output(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)


def decision(candidate, ok=True, **overrides):
    return dict(candidate=candidate, decision="pass" if ok else "revise",
                reason="已检查词项替代关系", terms_substitutable=True, aliases_belong=True,
                source_supported=True) | overrides


def execute(outputs, task=None, point=None):
    client = StubModelClient(*(json.dumps(x, ensure_ascii=False) for x in outputs))
    agent = SearchPlannerAgent(client, concept_review=True)
    return agent.plan(point or make_point(), task or make_task()), client


@pytest.mark.parametrize("kind", ["omission", "misplaced_alias", "contradictory_pass"])
def test_targeted_repair(kind):
    draft = valid_draft(make_task())
    good = draft["concepts"]
    bad = [c.copy() for c in good]
    if kind == "omission":
        bad.pop()
        initial = {"decisions": [decision(k+1) for k in range(len(bad))], "missing": ["遗漏训练通信开销"]}
        key = len(good)
    else:
        bad[0]["alias"] = ["不相关机制"]
        initial = {"decisions": [decision(1, kind=="contradictory_pass", aliases_belong=False),
                                 *[decision(k) for k in range(2, len(good)+1)]], "missing": []}
        key = 1
    plan, client = execute([{"concepts": bad}, initial, {"concept": good[key-1]},
                           {"decisions": [decision(key)], "missing":[]}, {"strategies": draft["strategies"]}])
    assert len(client.calls) == 5
    assert len(plan.concepts) == len(good)
    assert all("不相关机制" not in c.alias for c in plan.concepts)
    assert '"candidates":' not in client.calls[2][0][1].content
    assert '"check_coverage": false' in client.calls[3][0][1].content


def test_empty_pass_set_stops_after_three_rounds():
    c = valid_draft(make_task())["concepts"][0]
    rejected = {"decisions":[decision(1,False)], "missing":[]}
    with pytest.raises(SearchPlannerExhaustedError, match="通过集为空"):
        execute([{"concepts":[c]}, rejected, *([{"concept":c}, rejected]*3)])


def test_missing_decision_stops_before_strategy():
    with pytest.raises(SearchPlannerExhaustedError, match="逐项覆盖"):
        execute([{"concepts":valid_draft(make_task())["concepts"]}, {"decisions":[], "missing":[]}])


@pytest.mark.parametrize("lang,present,compare", [("zh",True,False),("en",True,False),("en",False,True)])
def test_task_language_and_comparison(lang, present, compare):
    task = make_task(language=lang)
    draft = valid_draft(task)
    batch = {"concepts":draft["concepts"], "comparison":valid_draft(make_task())["concepts"] if compare else []}
    plan, client = execute([batch, {"decisions":[decision(k+1) for k in range(len(batch["concepts"]))],"missing":[]},
                            {"strategies":draft["strategies"]}], task, make_point(english=present))
    assert plan.task_id == task.task_id
    assert f'"needs_translation": {str(compare).lower()}' in client.calls[0][0][1].content


def test_remaining_risk_coherent_false_pass():
    draft = valid_draft(make_task())
    draft["concepts"][0]["alias"] = ["注意力层"]
    plan, _ = execute([{"concepts":draft["concepts"]},
                       {"decisions":[decision(k+1) for k in range(len(draft["concepts"]))],"missing":[]},
                       {"strategies":draft["strategies"]}])
    assert "注意力层" in plan.concepts[0].alias


def test_three_failed_repairs_keep_only_passed_concepts():
    draft = valid_draft(make_task())
    bad = draft["concepts"][-1]
    n = len(draft["concepts"])
    initial = {"decisions":[decision(k, k!=n) for k in range(1,n+1)],"missing":[]}
    revise = [{"concept":bad}, {"decisions":[decision(n,False)],"missing":[]}]
    plan, client = execute([{"concepts":draft["concepts"]}, initial, *(revise*3),
                            {"strategies":[{"level":v} for v in ("strict","medium","broad")]}])
    assert len(plan.concepts) == n-1
    assert len(client.calls) == 9


def test_persisted_success_can_be_checked_without_debug(tmp_path, monkeypatch):
    from novelty_agent_framework.core.concept_audit import read_attempt
    monkeypatch.chdir(tmp_path)
    draft = valid_draft(make_task())
    execute([{"concepts":draft["concepts"]},
             {"decisions":[decision(k+1) for k in range(len(draft["concepts"]))], "missing":[]},
             {"strategies":draft["strategies"]}])
    index_path, = tmp_path.rglob("index.json")
    index = json.loads(index_path.read_text())
    binding = {k:index[k] for k in ("run_id","point_id","task_id")}
    records = read_attempt(index_path.parent, **binding)
    assert {"events.jsonl","plan.json","summary.json","input.json"} <= records.keys()
    (index_path.parent/"plan.json").write_text("{}")
    with pytest.raises(ValueError, match="摘要"):
        read_attempt(index_path.parent, **binding)


def test_simultaneous_tasks_keep_their_own_catalog():
    import asyncio
    from backend.env import ModelResponse

    class ParallelClient:
        async def acomplete(self, messages, *, options):
            await asyncio.sleep(0)
            user = messages[1].content
            language = "en" if '"language": "en"' in user else "zh"
            draft = valid_draft(make_task(language=language))
            if len(messages) == 3:
                data = {"strategies":draft["strategies"]}
            elif "check_coverage" in user:
                data = {"decisions":[decision(k+1) for k in range(len(draft["concepts"]))], "missing":[]}
            else:
                data = {"concepts":draft["concepts"]}
            return ModelResponse(content=json.dumps(data))

    async def both():
        agent = SearchPlannerAgent(ParallelClient(), concept_review=True)
        return await asyncio.gather(*(agent.aplan(make_point(), make_task(language=lang)) for lang in ("zh","en")))

    chinese, english = asyncio.run(both())
    assert chinese.task_id != english.task_id
    assert chinese.concepts[0].terms != english.concepts[0].terms


def test_schema_crop_and_retry_are_separate_from_semantic_budget():
    draft = valid_draft(make_task())
    batch = {"concepts":draft["concepts"], "strategies":[]}
    reviews = {"decisions":[decision(k+1) for k in range(len(draft["concepts"]))], "missing":[]}
    plan, client = execute([batch, {"decisions":[], "missing":[]}, reviews, {"strategies":draft["strategies"]}])
    assert plan.concepts and len(client.calls) == 4


def test_english_only_source_for_chinese_task():
    point = make_point().model_copy(update={"claim":"","technical_features":[]})
    draft = valid_draft(make_task())
    batch = {"concepts":draft["concepts"], "comparison":valid_draft(make_task(language="en"))["concepts"]}
    plan, _ = execute([batch, {"decisions":[decision(k+1) for k in range(len(draft["concepts"]))],"missing":[]},
                       {"strategies":draft["strategies"]}], point=point)
    assert plan.concepts[0].terms == draft["concepts"][0]["terms"]
