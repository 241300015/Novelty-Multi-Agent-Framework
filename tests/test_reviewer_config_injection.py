"""Exercise configuration through the actual prompt and model call boundaries."""
import json
from types import SimpleNamespace

import pytest
from backend.env import ModelResponse
from novelty_agent_framework.config import build_workflow, load_application_config
from novelty_agent_framework.config.loader import legacy_shape
from novelty_agent_framework.schemas import EvidenceCard, EvidenceSource
from novelty_agent_framework.response_language import with_chinese_explanations


@pytest.mark.parametrize("legacy", [False, True])
def test_reviewer_config_reaches_prompt_and_client(legacy):
    config = load_application_config(environ={})
    config.reviewer.enabled = True
    config.reviewer.prompt = "reviewer/custom"
    config.reviewer.model.temperature = 0.17
    config.reviewer.model.max_tokens = 1234
    config.reviewer.model.timeout_seconds = 27
    reviewer = build_workflow(legacy_shape(config) if legacy else config).services.reviewer
    captured = {}

    class Prompts:
        def render(self, name, **variables):
            captured["prompt"] = name
            return SimpleNamespace(system="custom system", user=variables["cards_json"])

    class Client:
        def complete(self, messages, *, options):
            captured["options"] = options
            captured["system"] = messages[0].content
            return ModelResponse(content=json.dumps({"decisions": [{
                "card_id": "C", "verdict": "accept", "reviewed_confidence": 0.8,
            }]}))

    reviewer._prompts = Prompts()
    reviewer.model_client = Client()
    card = EvidenceCard(card_id="C", task_id="T", novelty_point_id="P",
                        document_title="Paper", main_contribution="Claim",
                        sources=[EvidenceSource(title="Paper", quote="Claim")],
                        confidence=0.8, relevance=0.8)
    result = reviewer.review([card], points=[], tasks=[])
    assert result.accepted[0] is card
    assert captured["prompt"] == "reviewer/custom"
    assert captured["system"] == with_chinese_explanations("custom system")
    assert captured["options"].temperature == 0.17
    assert captured["options"].max_tokens == 1234
    assert captured["options"].timeout_seconds == 27


@pytest.mark.parametrize("legacy", [False, True])
def test_disabled_reviewer_is_passthrough_composition(legacy):
    config = load_application_config(environ={})
    config.reviewer.enabled = False
    assert build_workflow(legacy_shape(config) if legacy else config).services.reviewer is None


@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize("controlled", [False, True])
def test_controlled_card_config_survives_both_composition_roots(legacy, controlled):
    config = load_application_config(environ={})
    config.reviewer.controlled_reading = controlled
    config.reviewer.support_prompt = "reviewer/custom_support"
    service = build_workflow(legacy_shape(config) if legacy else config).services.reviewer
    assert service.config.controlled_reading is controlled
    assert service.config.support_prompt_name == "reviewer/custom_support"


@pytest.mark.parametrize("legacy", [False, True])
def test_summary_budgets_are_independent_of_card_budgets(legacy):
    config = load_application_config(environ={})
    config.reviewer.max_steps = 1
    config.reviewer.summary_max_calls = 5
    config.reviewer.summary_max_rechecks = 3
    config.reviewer.summary_max_recheck_chars = 24000
    service = build_workflow(legacy_shape(config) if legacy else config).services.reviewer
    assert service.config.max_steps == 1
    assert service.config.summary_max_calls == 5
    assert service.config.summary_max_rechecks == 3
    assert service.config.summary_max_recheck_chars == 24000
