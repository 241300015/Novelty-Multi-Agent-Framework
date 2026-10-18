import json

from novelty_agent_framework.config import build_workflow, effective_safe_config
from novelty_agent_framework.config.loader import (
    DEFAULT_RESEARCHER_PATH,
    DEFAULT_SEARCH_PLANNER_PATH,
    load_application_config,
)


def test_perturbed_config_reaches_runtime_objects(tmp_path):
    researcher_raw = json.loads(DEFAULT_RESEARCHER_PATH.read_text(encoding="utf-8"))
    researcher_raw["model"].update(temperature=0.17, max_tokens=321, timeout_seconds=12)
    researcher_raw["harness"].update(max_turns=7, max_total_tool_calls=6)
    researcher_raw["harness"]["per_tool_limits"]["browser"] = 2
    database = researcher_raw["tools"]["database_search"]
    database.update(candidate_limit_per_task=3, full_text_limit_per_task=2, max_concurrency=5)
    database["providers"]["arxiv"].update(
        search_transport="api",  # These assertions exercise API-specific retry settings.
        min_interval_seconds=0.07, timeout_seconds=4, max_retries=1,
        max_retry_delay_seconds=2, retry_budget_seconds=7,
        circuit_failure_threshold=3, circuit_cooldown_seconds=11,
        full_text_max_chars=4321,
    )
    # 默认配置关闭了 web_search 与 browser；本用例验证配置注入，因此显式打开它们
    researcher_raw["tools"]["web_search"].update(
        enabled=True, default_max_results=4, max_results_per_call=6
    )
    researcher_raw["tools"]["web_search"]["baidu"]["timeout_seconds"] = 7
    researcher_raw["tools"]["browser"].update(
        enabled=True,
        network_mode="direct",
        navigation_timeout_ms=4321,
        max_html_chars=2222,
        max_text_chars=1111,
    )
    researcher_raw["tools"]["reader"].update(
        default_chars_per_read=1000, max_chars_per_read=1234,
        max_total_read_chars=2345,
    )
    planner_raw = json.loads(DEFAULT_SEARCH_PLANNER_PATH.read_text(encoding="utf-8"))
    planner_raw["model"].update(temperature=0.19, max_tokens=654, timeout_seconds=13)
    planner_raw["max_attempts"] = 1
    planner_raw["prompt"] = "search_planner/custom"
    researcher_path = tmp_path / "researcher.json"
    planner_path = tmp_path / "planner.json"
    researcher_path.write_text(json.dumps(researcher_raw), encoding="utf-8")
    planner_path.write_text(json.dumps(planner_raw), encoding="utf-8")

    config = load_application_config(
        researcher_path=researcher_path, search_planner_path=planner_path
    )
    workflow = build_workflow(config)
    task = workflow.services.task_researcher
    database_tool = task.tools.get("database_search")
    internal = database_tool.tools_by_source["arxiv"]
    web = task.tools.get("web_search")
    browser = task.tools.get("browser")
    reader = task.tools.get("reader")
    planner = workflow.services.search_planner

    assert task.config.max_steps == 7 and task.config.max_tool_calls == 6
    assert task.harness.config.per_tool_limits["browser"] == 2
    assert task.harness.config.max_total_read_chars == 2345
    assert task.config.model_options.temperature == 0.17
    assert task.config.model_options.max_tokens == 321
    assert task.config.model_options.timeout_seconds == 12
    assert internal.candidate_limit == 3 and internal.full_text_limit == 2
    assert internal.max_concurrency == 5
    assert internal.source.search_tool._min_interval == 0.07
    assert internal.source.search_tool._max_retries == 1
    assert internal.source.search_tool._max_retry_delay == 2
    assert internal.source.search_tool._retry_budget_seconds == 7
    assert internal.source.search_tool._circuit_failure_threshold == 3
    assert internal.source.search_tool._circuit_cooldown_seconds == 11
    assert internal.source.full_text_tool._max_chars == 4321
    assert web.default_max_results == 4 and web.max_results_per_call == 6
    assert web.backend.timeout_seconds == 7
    assert browser.backend.navigation_timeout_ms == 4321
    assert browser.backend.network_mode == "direct"
    assert browser.backend.limits.html_chars == 2222
    assert browser.backend.limits.text_chars == 1111
    assert reader.default_chars_per_read == 1000
    assert reader.reader.max_chars_per_read == 1234
    assert planner.max_attempts == 1
    assert planner.prompt_name == "search_planner/custom"
    assert planner.model_options.temperature == 0.19
    assert planner.model_options.max_tokens == 654
    assert planner.model_options.timeout_seconds == 13

    safe = effective_safe_config(config)
    assert safe["workflow"] == config.project.workflow.model_dump(mode="json")
    assert safe["coordinator"]["model"]["alias"] == config.coordinator.model.alias
    assert (
        safe["point_extractor"]["model"]["alias"]
        == config.point_extractor.model.alias
    )
    serialized = json.dumps(safe)
    assert "api_key\"" not in serialized and "secret" not in serialized.lower()


def test_default_runtime_is_english_only_with_expanded_tool_budgets():
    config = load_application_config()
    workflow = build_workflow(config)

    assert workflow.services.coordinator.research_languages == ("en",)
    assert workflow.services.task_researcher.config.max_steps == 28
    assert workflow.services.task_researcher.config.max_tool_calls == 24
    assert workflow.services.task_researcher.config.per_tool_limits == {
        "reference_search": 8,
        "database_search": 8,
        "web_search": 5,
        "browser": 3,
        "reader": 16,
    }
    assert workflow.services.reviewer.config.max_steps == 14
    assert workflow.services.reviewer.config.max_tool_calls == 12
    assert workflow.services.reviewer.config.max_total_read_chars == 96_000
