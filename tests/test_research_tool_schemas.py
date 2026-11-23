"""Researcher workflow and concrete-tool schema boundary tests."""

from __future__ import annotations

import inspect

import pytest
from pydantic import ValidationError

from novelty_agent_framework.schemas import (
    BrowserArguments,
    EvidenceCardBuilderRequest,
    ResearchFinishDraft,
    ReaderArguments,
    ReferenceReaderToolArguments,
    WebSearchArguments,
)
from novelty_agent_framework.schemas import research, research_tools
from novelty_agent_framework.tools import ReaderTool


def test_tool_schema_module_imports_without_implementations() -> None:
    assert WebSearchArguments(query="graph sampling").max_results == 10
    assert BrowserArguments(source_record_id="src_1").source_record_id == "src_1"
    request = EvidenceCardBuilderRequest(
        draft=ResearchFinishDraft(cards=[], no_evidence_reason="none found")
    )
    assert request.draft.no_evidence_reason == "none found"


def test_reader_uses_canonical_arguments_and_legacy_alias() -> None:
    assert issubclass(ReaderTool.args_schema, research_tools.ReaderCallArguments)
    public = ReaderTool.args_schema.model_json_schema()["properties"]
    assert "candidate" in public and "request" not in public
    assert ReferenceReaderToolArguments is ReaderArguments
    with pytest.raises(ValidationError):
        ReaderTool.args_schema.model_validate(
            {"artifact_id": "art_1", "unexpected": True}
        )


def test_workflow_schema_does_not_define_concrete_tool_arguments() -> None:
    source = inspect.getsource(research)
    assert "class ReaderArguments" not in source
    assert "class ReferenceReaderToolArguments" not in source
    assert "class StructuredRetrievalToolArguments" not in source
    assert research_tools.ReaderArguments.__module__.endswith("research_tools")
