"""Exercise configuration through the actual prompt and model call boundaries."""
import json
from types import SimpleNamespace

import pytest
from backend.env import ModelResponse
from novelty_agent_framework.config import build_workflow, load_application_config
from novelty_agent_framework.config.loader import legacy_shape
from novelty_agent_framework.schemas import EvidenceCard, EvidenceSource


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
    assert captured["system"] == "custom system"
    assert captured["options"].temperature == 0.17
    assert captured["options"].max_tokens == 1234
    assert captured["options"].timeout_seconds == 27


@pytest.mark.parametrize("legacy", [False, True])
def test_disabled_reviewer_is_passthrough_composition(legacy):
    config = load_application_config(environ={})
    config.reviewer.enabled = False
    assert build_workflow(legacy_shape(config) if legacy else config).services.reviewer is None


@pytest.mark.parametrize("legacy", [False, True])
def test_reviewer_and_researcher_share_database_acquisition_backend(legacy):
    config = load_application_config(environ={})
    workflow = build_workflow(legacy_shape(config) if legacy else config)
    reviewer_adapter = workflow.services.reviewer.tools.get("database_search")
    researcher_database = workflow.services.task_researcher.tools.get("database_search")
    assert reviewer_adapter.database_search is researcher_database
    assert reviewer_adapter.reference_store is researcher_database.reference_store
    assert workflow.services.reviewer.harness.config.per_tool_limits[
        "database_search"
    ] == config.reviewer.max_fulltext_calls
