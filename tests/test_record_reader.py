"""Record reads through the production registry and its model-facing projection."""
import asyncio

import pytest

from novelty_agent_framework.schemas.reader import ReaderOpen
from novelty_agent_framework.schemas.references import AccessStatus
from novelty_agent_framework.schemas.research import TargetPaperIdentity
from novelty_agent_framework.tools import ReaderTool, ReferenceArtifactReaderTool, ResearcherToolRegistry
from novelty_agent_framework.tools.reader import ReviewerReaderTool
from novelty_agent_framework.core.tool_call_harness import ToolCallHarness, ToolCallHarnessConfig, ToolCallBudgetExhausted
from test_reader_tool_call_harness_integration import (
    PAPER_ID, prepare_store, research_scope, ScriptedModelClient, tool_request, ModelResponse,
)


def setup_reader(path, abstract="a中 b\ncd"):
    store = prepare_store(path)
    manifest = store.load_manifest(PAPER_ID)
    manifest.source_records[0].abstract = abstract
    store.persist_manifest(PAPER_ID, manifest)
    reader = ReferenceArtifactReaderTool(store)
    return store, ResearcherToolRegistry([ReaderTool(reader)])


def invoke(registry, scope=None, **kwargs):
    observation = asyncio.run(registry.execute("reader", {"request": kwargs}, scope=scope or research_scope()))
    return registry.project_model_context("reader", observation)


def open_record(registry):
    return invoke(registry, operation="open", source_record_id="src_test", part="abstract")["handle"]


def test_paginate_reopen_and_replay(tmp_path):
    store, registry = setup_reader(tmp_path)
    handle = open_record(registry)
    request = dict(operation="read", handle=handle, max_tokens=4)
    page = invoke(registry, **request)
    blocks = list(page["blocks"])
    while page["next_cursor"]:
        page = invoke(registry, **request, cursor=page["next_cursor"])
        blocks.extend(page["blocks"])
    assert "".join(b["content"] for b in blocks) == "a中 b\ncd"
    assert all(len(b["content"].encode()) <= 4 for b in blocks)
    block = blocks[1]
    citation = {k: block[k] for k in ("block_id", "locator")}
    # Restart with fresh instances and an unrelated manifest metadata update.
    manifest = store.load_manifest(PAPER_ID)
    manifest.works[0].title = "Updated title"
    store.persist_manifest(PAPER_ID, manifest)
    registry = ResearcherToolRegistry([ReaderTool(ReferenceArtifactReaderTool(store))])
    replay = invoke(registry, **request, citation=citation)
    assert replay["blocks"] == [block]
    invalid = dict(citation, block_id="0" * 64)
    assert invoke(registry, **request, citation=invalid)["status"] == "invalid_address"
    manifest.source_records[0].abstract += "!"
    store.persist_manifest(PAPER_ID, manifest)
    assert invoke(registry, **request, citation=citation)["status"] == "stale_handle"


def test_scope_budget_and_availability(tmp_path):
    store, reg = setup_reader(tmp_path, "中文")
    handle = open_record(reg)
    assert invoke(reg, operation="read", handle=handle, max_tokens=1)["status"] == "budget_too_small"
    for key, value in (("subject_paper_id", "other"), ("namespace", "subject_reference")):
        assert invoke(reg, operation="read", handle=dict(handle, **{key: value}))["status"] == "invalid_address"
    assert invoke(reg, operation="read", handle=handle, cursor={"offset": 1, "handle_digest": "0"*64})["status"] == "invalid_address"
    assert invoke(reg, operation="open", source_record_id="src_test", part="full_text")["status"] == "unsupported_format"
    assert invoke(reg, operation="open", source_record_id="missing", part="abstract")["status"] == "unavailable"
    scope = research_scope().model_copy(update={"target_identity": TargetPaperIdentity(title="Harness Test Paper")})
    assert invoke(reg, scope, operation="read", handle=handle)["succeeded"] is False
    reviewer = ReviewerReaderTool(ReferenceArtifactReaderTool(store))
    with pytest.raises(ValueError):
        reviewer.args_schema.model_validate({"request": ReaderOpen(operation="open", part="abstract", source_record_id="src_test")})


@pytest.mark.parametrize("budget", [4, 5])
def test_harness_accounts_for_read_and_citation(tmp_path, budget):
    _, reg = setup_reader(tmp_path, "abcd")
    handle = open_record(reg)
    request = dict(operation="read", handle=handle, max_tokens=3)
    first = invoke(reg, **request)
    model = ScriptedModelClient(
        tool_request({"request": dict(operation="open", source_record_id="src_test", part="abstract")}),
        tool_request({"request": request}, "r2"),
        tool_request({"request": dict(request, max_tokens=2, cursor=first["next_cursor"])}, "r3"),
        ModelResponse(content="done"),
    )
    harness = ToolCallHarness(model, reg, config=ToolCallHarnessConfig(max_total_read_chars=budget))
    run = harness.run(system_prompt="test", initial_user_message="test", scope=research_scope())
    if budget == 4:
        with pytest.raises(ToolCallBudgetExhausted):
            asyncio.run(run)
    else:
        assert asyncio.run(run).final_content == "done"


def test_normalized_call_roundtrips_and_unmerged_abstract(tmp_path):
    store, reg = setup_reader(tmp_path)
    manifest = store.load_manifest(PAPER_ID)
    orphan = manifest.source_records[0].model_copy(update={"source_record_id": "new", "work_id": None, "access_status": AccessStatus.METADATA_ONLY})
    manifest.source_records.append(orphan)
    store.persist_manifest(PAPER_ID, manifest)
    call = reg.validate_arguments("reader", {"request": {"operation": "open", "part": "abstract", "source_record_id": "new"}})
    assert reg.validate_arguments("reader", call.model_dump(mode="json")) == call
    handle = invoke(reg, **call.request.model_dump())["handle"]
    assert invoke(reg, operation="read", handle=handle)["blocks"][0]["content"] == orphan.abstract


def test_candidate_abstract_pages_remain_verifiable_after_restart(tmp_path):
    store, registry = setup_reader(tmp_path, "alpha beta")
    candidate = {"source_record_id": "src_test", "part": "abstract"}
    def run(address):
        return asyncio.run(registry.execute("reader", {"candidate": address, "max_chars": 5}, scope=research_scope()))
    obs = run(candidate)
    assert obs.succeeded, obs.error
    page = obs.payload["candidate_page"]
    projection = registry.project_model_context("reader", obs)
    assert "handle" not in str(projection) and projection["read_result"]["text"] == "alpha"
    schema = registry.descriptions()[0]["arguments_schema"]["properties"]
    assert "request" not in schema and "candidate" in schema
    continuation = page["continuation"]
    assert run(dict(candidate, continuation=continuation)).payload["read_result"]["text"] == " beta"
    assert not run(dict(candidate, part="full_text", continuation=continuation)).succeeded
    manifest = store.load_manifest(PAPER_ID)
    artifact = next(a for a in manifest.artifacts if a.source_field == "abstract")
    assert artifact.relative_path == "list.json"
    reread = store.read_document_slice(PAPER_ID, artifact_id=artifact.artifact_id, char_start=0, max_chars=5)
    assert reread.text == "alpha"
    manifest.source_records[0].abstract = "changed"
    store.persist_manifest(PAPER_ID, manifest)
    with pytest.raises(ValueError, match="sha256"):
        store.verify_artifact_file(PAPER_ID, artifact.artifact_id)
