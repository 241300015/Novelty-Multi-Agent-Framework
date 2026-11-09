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



def translate_review(payload, verdicts):
    point = payload["point"]
    quote = next((x for x in [point.get("claim"), point.get("claim_en"),
                            *point.get("technical_features", []),
                            *point.get("technical_features_en", [])] if x), "")
    converted = []
    for verdict in verdicts:
        key = str(verdict["candidate"])
        candidate = payload["candidates"].get(key)
        if candidate is None:
            continue
        ok = verdict["decision"] == "pass"
        terms = [{"term": t, "substitutable": ok, "reason": "检查词项含义"}
                 for t in candidate["terms"]]
        aliases_ok = verdict["aliases_belong"] and verdict["decision"] == "pass"
        aliases = [{"alias": a, "substitutable": aliases_ok, "reason": "检查别名"}
                   for a in candidate.get("alias", [])]
        item = {"candidate": verdict["candidate"], "source_supported": True,
                "source_quote": quote, "source_reason": "输入支持该概念",
                "role_correct": ok if not aliases else True, "role_reason": "分类符合概念" if ok else "分类错误",
                "translation_consistent": True if payload["comparison"] else None,
                "translation_reason": "补译保留原意" if payload["comparison"] else "",
                "terms": terms, "aliases": aliases}
        if not payload["check_coverage"]:
            before = payload.get("previous_candidate")
            item["issue_resolution"] = ("unresolved" if not ok else
                                        "fixed" if before != candidate else "invalid")
            item["resolution_reason"] = "已核对原问题"
        converted.append(item)
    return converted


class ReviewAdapter(StubModelClient):
    def complete(self, messages, *, options=None):
        from backend.env import ModelResponse
        self.calls.append((list(messages), options))
        index = min(len(self.calls) - 1, len(self.outputs) - 1)
        result = json.loads(self.outputs[index])
        if "decisions" in result and "check_coverage" in messages[1].content:
            payload = json.JSONDecoder().raw_decode(messages[1].content)[0]
            result["decisions"] = translate_review(payload, result["decisions"])
        return ModelResponse(content=json.dumps(result, ensure_ascii=False))


def execute(outputs, task=None, point=None):
    client = ReviewAdapter(*(json.dumps(x, ensure_ascii=False) for x in outputs))
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
            if "冻结的概念目录" in messages[0].content:
                data = {"strategies":draft["strategies"]}
            elif "check_coverage" in user:
                payload = json.JSONDecoder().raw_decode(user)[0]
                data = {"decisions":translate_review(payload, [decision(k+1) for k in range(len(draft["concepts"]))]), "missing":[]}
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


def new_review(concepts, *, source_quote=None, resolution=None):
    if source_quote is None:
        source_quote = make_point().claim
    decisions = []
    for number, concept in concepts.items():
        item = {
            "candidate": number,
            "source_supported": True,
            "source_quote": source_quote,
            "source_reason": "输入支持该概念",
            "role_correct": True,
            "role_reason": "分类符合概念",
            "translation_consistent": None,
            "translation_reason": "",
            "terms": [{"term": value, "substitutable": True, "reason": "含义一致"}
                      for value in concept["terms"]],
            "aliases": [{"alias": value, "substitutable": True, "reason": "含义一致"}
                        for value in concept.get("alias", [])],
        }
        if resolution:
            item.update(issue_resolution=resolution, resolution_reason="核对具体问题")
        decisions.append(item)
    return {"decisions": decisions, "missing": []}


def execute_v2(outputs, *, point=None, task=None):
    client = StubModelClient(*(json.dumps(x, ensure_ascii=False) for x in outputs))
    agent = SearchPlannerAgent(client, concept_review=True)
    return agent.plan(point or make_point(), task or make_task()), client


def test_v2_empty_alias_skips_irrelevant_check():
    draft = valid_draft(make_task())
    concepts = draft["concepts"]
    for concept in concepts:
        concept["alias"] = []
    result, client = execute_v2([
        {"concepts": concepts},
        new_review({i: c for i, c in enumerate(concepts, 1)}),
        {"strategies": draft["strategies"]},
    ])
    assert len(result.concepts) == len(concepts)
    assert len(client.calls) == 3


@pytest.mark.parametrize("corruption", ["invented_quote", "missing_alias", "extra_term"])
def test_v2_invalid_evidence_or_field_coverage_is_repaired(corruption):
    draft = valid_draft(make_task())
    concepts = draft["concepts"]
    concepts[0]["alias"] = ["动态图神经网络"]
    bad = new_review({i: c for i, c in enumerate(concepts, 1)})
    if corruption == "invented_quote":
        bad["decisions"][0]["source_quote"] = "不存在于查新点的正文"
    elif corruption == "missing_alias":
        assert concepts[0]["alias"]
        bad["decisions"][0]["aliases"] = []
    else:
        bad["decisions"][0]["terms"].append(
            {"term": "模型额外添加的词项", "substitutable": True, "reason": "test"}
        )
    good = new_review({i: c for i, c in enumerate(concepts, 1)})
    result, client = execute_v2([{"concepts": concepts}, bad, good,
                                 {"strategies": draft["strategies"]}])
    assert len(result.concepts) == len(concepts)
    assert len(client.calls) == 4
    assert "纠正输出契约" in client.calls[2][0][1].content
    if corruption == "invented_quote":
        assert "候选 1 的 source_quote" in client.calls[2][0][1].content
        assert "不存在于查新点的正文" in client.calls[2][0][1].content


def test_v2_unchanged_fixed_is_not_accepted_and_problem_is_bound():
    draft = valid_draft(make_task())
    concepts = draft["concepts"]
    concepts[0]["alias"] = ["动态图神经网络"]
    first = new_review({i: c for i, c in enumerate(concepts, 1)})
    first["decisions"][0]["aliases"][0]["substitutable"] = False
    first["decisions"][0]["aliases"][0]["reason"] = "错配别名"
    candidate = concepts[0]
    unchanged = new_review({1: candidate}, resolution="fixed")
    invalid = new_review({1: candidate}, resolution="invalid")
    result, client = execute_v2([
        {"concepts": concepts}, first,
        {"concept": candidate}, unchanged,
        {"concept": candidate}, invalid,
        {"strategies": draft["strategies"]},
    ])
    assert result.concepts
    assert len(client.calls) == 7
    payload = json.JSONDecoder().raw_decode(client.calls[3][0][1].content)[0]
    assert "错配别名" in json.dumps(payload["problem"], ensure_ascii=False)
    assert payload["previous_candidate"]["terms"] == candidate["terms"]
    events_file, = __import__("pathlib").Path("outputs").rglob("events.jsonl")
    events = [json.loads(line) for line in events_file.read_text().splitlines()]
    resolved = [event["payload"] for event in events if event["step"] == "concept_issue_resolved"]
    assert [event["resolution"] for event in resolved] == ["unresolved", "invalid"]
    assert all(not event["changed"] for event in resolved)


def test_budget_exhausted_preserves_accepted_concepts_without_dispatch():
    from pathlib import Path
    from backend.env import ModelResponse
    from backend.env.model_client import ModelCallBudgetExceeded

    draft = valid_draft(make_task())
    concepts = draft["concepts"]
    initial = new_review({i: c for i, c in enumerate(concepts, 1)})
    initial["decisions"][0]["role_correct"] = False
    initial["decisions"][0]["role_reason"] = "分类错误"

    class BudgetClient:
        def __init__(self):
            self.calls = 0

        def complete(self, messages, *, options=None):
            self.calls += 1
            if self.calls == 1:
                return ModelResponse(content=json.dumps({"concepts": concepts}, ensure_ascii=False))
            if self.calls == 2:
                return ModelResponse(content=json.dumps(initial, ensure_ascii=False))
            raise ModelCallBudgetExceeded("预算不足，未派发")

    client = BudgetClient()
    planner = SearchPlannerAgent(client, concept_review=True)
    with pytest.raises(SearchPlannerExhaustedError, match="预算不足"):
        planner.plan(make_point(), make_task())
    summary_file, = Path("outputs").rglob("summary.json")
    summary = json.loads(summary_file.read_text())
    assert summary["status"] == "failed"
    assert summary["concept_calls"] == 2
    progress = summary["concept_result"]
    assert progress["accepted"] == list(range(2, len(concepts) + 1))
    assert "1" in progress["unresolved"]
    assert len(progress["accepted_concepts"]) == len(concepts) - 1
    assert not (summary_file.parent / "plan.json").exists()


def test_full_pass_set_with_missing_concept_keeps_partial_result():
    from pathlib import Path
    from novelty_agent_framework.agents.search_plan_compiler import SemanticLimits

    draft = valid_draft(make_task())
    concepts = draft["concepts"]
    initial = new_review({i: c for i, c in enumerate(concepts, 1)})
    initial["missing"] = ["遗漏的关键机制"]
    new = {"role": "method", "terms": ["图摘要技术"], "alias": [], "importance": 3}
    responses = [
        {"concepts": concepts}, initial, {"concept": new},
        new_review({len(concepts) + 1: new}, resolution="fixed"),
        {"strategies": draft["strategies"]},
    ]
    client = StubModelClient(*(json.dumps(v, ensure_ascii=False) for v in responses))
    planner = SearchPlannerAgent(client, concept_review=True,
                                 semantic_limits=SemanticLimits(max_concepts=len(concepts)))
    plan = planner.plan(make_point(), make_task())
    assert len(plan.concepts) == len(concepts)
    assert all("图摘要技术" not in c.terms for c in plan.concepts)
    summary_file, = Path("outputs").rglob("summary.json")
    summary = json.loads(summary_file.read_text())
    assert summary["status"] == "partial"
    assert str(len(concepts) + 1) in summary["concept_result"]["unresolved"]


def test_more_generated_concepts_than_limit_preserves_partial_set():
    from pathlib import Path
    from novelty_agent_framework.agents.search_plan_compiler import SemanticLimits

    draft = valid_draft(make_task())
    concepts = draft["concepts"] + [{"role": "method", "terms": ["图摘要技术"], "importance": 3}]
    initial = new_review({i: c for i, c in enumerate(concepts, 1)})
    responses = [{"concepts": concepts}, initial, {"strategies": draft["strategies"]}]
    client = StubModelClient(*(json.dumps(v, ensure_ascii=False) for v in responses))
    planner = SearchPlannerAgent(client, concept_review=True,
                                 semantic_limits=SemanticLimits(max_concepts=len(concepts) - 1))
    plan = planner.plan(make_point(), make_task())
    assert len(plan.concepts) == len(concepts) - 1
    summary_file, = Path("outputs").rglob("summary.json")
    assert str(len(concepts)) in json.loads(summary_file.read_text())["concept_result"]["unresolved"]


@pytest.mark.parametrize("kind", ["role", "source", "translation"])
def test_bad_review_routes_specific_reason_into_correction(kind):
    draft = valid_draft(make_task())
    concepts = draft["concepts"]
    task = make_task(language="en") if kind == "translation" else make_task()
    if kind == "translation":
        draft = valid_draft(task)
        concepts = draft["concepts"]
    point = make_point(english=kind != "translation")
    comparison = valid_draft(make_task())["concepts"] if kind == "translation" else []
    initial = new_review({i: c for i, c in enumerate(concepts, 1)})
    for item in initial["decisions"]:
        if comparison:
            item["translation_consistent"] = True
            item["translation_reason"] = "译义一致"
    first = initial["decisions"][0]
    if kind == "role":
        first["role_correct"] = False
        first["role_reason"] = "分类应为研究对象"
        expected = first["role_reason"]
    elif kind == "source":
        first["source_supported"] = False
        first["source_reason"] = "输入没有支持该机制"
        expected = first["source_reason"]
    else:
        first["translation_consistent"] = False
        first["translation_reason"] = "补译遗漏动态特性"
        expected = first["translation_reason"]
    fixed = concepts[0] | {"role": "method" if concepts[0]["role"] != "method" else "feature"}
    local = new_review({1: fixed}, resolution="fixed")
    if comparison:
        local["decisions"][0]["translation_consistent"] = True
        local["decisions"][0]["translation_reason"] = "译义一致"
    responses = [{"concepts": concepts, "comparison": comparison}, initial, {"concept": fixed},
                 local, {"strategies": draft["strategies"]}]
    client = StubModelClient(*(json.dumps(v, ensure_ascii=False) for v in responses))
    plan = SearchPlannerAgent(client, concept_review=True).plan(point, task)
    assert plan.concepts
    assert expected in client.calls[2][0][1].content
    assert expected in client.calls[3][0][1].content


def test_translation_review_cannot_silently_omit_judgment():
    draft = valid_draft(make_task(language="en"))
    concepts = draft["concepts"]
    comparison = valid_draft(make_task())["concepts"]
    bad = new_review({i: c for i, c in enumerate(concepts, 1)})
    good = new_review({i: c for i, c in enumerate(concepts, 1)})
    for item in good["decisions"]:
        item["translation_consistent"] = True
        item["translation_reason"] = "译义一致"
    responses = [{"concepts": concepts, "comparison": comparison}, bad, good,
                 {"strategies": draft["strategies"]}]
    client = StubModelClient(*(json.dumps(v, ensure_ascii=False) for v in responses))
    plan = SearchPlannerAgent(client, concept_review=True).plan(make_point(english=False), make_task(language="en"))
    assert plan.concepts
    assert "纠正输出契约" in client.calls[2][0][1].content


def test_strategy_prompt_receives_only_frozen_concepts_and_strategy_contract():
    draft = valid_draft(make_task())
    concepts = draft["concepts"]
    plan, client = execute_v2([
        {"concepts": concepts},
        new_review({i: c for i, c in enumerate(concepts, 1)}),
        {"strategies": draft["strategies"]},
    ])
    assert plan.strategies
    messages, _ = client.calls[-1]
    assert len(messages) == 2
    assert "strategies" in messages[0].content
    assert "不得生成或修改概念" in messages[0].content
    payload = json.loads(messages[1].content)
    assert len(payload["concepts"]) == len(concepts)
    assert [c["concept_id"] for c in payload["concepts"]] == [
        f"C{i}" for i in range(1, len(concepts) + 1)
    ]


def test_direct_legacy_mode_keeps_original_single_draft_contract():
    task = make_task()
    draft = valid_draft(task)
    client = StubModelClient(json.dumps(draft, ensure_ascii=False))
    plan = SearchPlannerAgent(client).plan(make_point(), task)
    assert plan.concepts
    assert len(client.calls) == 1


def test_strategy_prompt_override_is_rendered_for_the_frozen_stage():
    from types import SimpleNamespace

    class PromptSpy:
        def __init__(self):
            self.rendered = []

        def render(self, name, **variables):
            self.rendered.append((name, variables))
            return SimpleNamespace(system="用户策略模板" if name == "custom/strategy" else name,
                                   user=variables.get("payload_json", ""))

    draft = valid_draft(make_task())
    concepts = draft["concepts"]
    outputs = [
        {"concepts": concepts},
        new_review({i: c for i, c in enumerate(concepts, 1)}),
        {"strategies": draft["strategies"]},
    ]
    client = StubModelClient(*(json.dumps(v, ensure_ascii=False) for v in outputs))
    prompts = PromptSpy()
    planner = SearchPlannerAgent(client, prompts=prompts, concept_review=True,
                                 strategy_prompt_name="custom/strategy")
    plan = planner.plan(make_point(), make_task())
    assert plan.strategies
    strategy_name, strategy_variables = prompts.rendered[-1]
    assert strategy_name == "custom/strategy"
    assert json.loads(strategy_variables["payload_json"])["concepts"][0]["concept_id"] == "C1"
    assert "用户策略模板" in client.calls[-1][0][0].content


def test_strategy_retry_feedback_keeps_frozen_catalog_and_avoids_old_prompt():
    point = make_point()
    task = make_task()
    concepts = valid_draft(task)["concepts"]
    planner = SearchPlannerAgent(StubModelClient("unused"), concept_review=True)
    feedback = json.dumps({"fields": {"field_0": {"path": ["strategies", 0, "focus_concepts"]}}},
                          ensure_ascii=False)
    messages = planner._build_strategy_messages(point=point, task=task, concepts=concepts,
                                                retry_reason=feedback)
    assert "updates" in messages[0].content
    assert "SearchPlanDraft" not in messages[0].content
    assert "生成或修改概念" in messages[0].content
    assert "修复反馈" in messages[1].content
    assert all(f'"concept_id": "C{i}"' in messages[1].content
               for i in range(1, len(concepts) + 1))


def test_single_term_does_not_require_alternative_words():
    from novelty_agent_framework.core.concept_stage import Verdict, verdict_problem
    verdict = Verdict(candidate=1, source_supported=True, source_quote="图摘要",
        source_reason="原文支持", role_correct=True, role_reason="分类合理",
        translation_consistent=None, translation_reason="",
        terms=[{"term": "图摘要", "substitutable": False, "reason": "没有替代词"}],
        aliases=[])
    assert verdict_problem(verdict) == ""
    verdict.source_supported = False
    assert "查新点缺少支撑" in verdict_problem(verdict)
