"""Reviewer read failures remain mechanical facts, not semantic absence."""
import asyncio
import json
import shutil
from pathlib import Path

import pytest
from backend.env import ModelResponse, ModelToolCall
from novelty_agent_framework.agents import NoveltyEvidenceReviewer
from novelty_agent_framework.persistence import ReferenceStore
from novelty_agent_framework.schemas import ReaderArguments
from novelty_agent_framework.tools import ReviewerReaderTool, ResearcherToolRegistry
from novelty_agent_framework.tools.reference_reader import ReferenceArtifactReaderTool
from test_novelty_point_reviewer import ScriptedClient, RecordingReader, _request, _review_json, _reviewer
from test_reviewer_evidence_repair import fixed_request, ROOT, BODY_ID


def test_failed_reader_then_valid_insufficient_is_not_pure_semantic():
    client = ScriptedClient(ModelResponse(content=None, tool_calls=(ModelToolCall(
        id='bad', name='reader', arguments={'artifact_id': 'outside'}),)),
        ModelResponse(content=_review_json('insufficient_evidence')))
    result = asyncio.run(_reviewer(client, RecordingReader()).review(_request()))
    assert result.incomplete_reason == 'technical_error'
    assert result.execution_issues[0].code == 'tool.scope'
    assert result.verdict is None


def test_unneeded_failed_read_does_not_override_valid_reviewed_result():
    client = ScriptedClient(ModelResponse(content=None, tool_calls=(ModelToolCall(
        id='bad', name='reader', arguments={'artifact_id': 'outside'}),)),
        ModelResponse(content=_review_json()))
    result = asyncio.run(_reviewer(client, RecordingReader()).review(_request()))
    assert result.status.value == 'reviewed'
    assert result.verdict.value == 'partially_novel'
    assert result.incomplete_reason is None
    assert result.execution_issues[0].code == 'tool.scope'


def local_tool(tmp_path):
    request = fixed_request()
    shutil.copytree(ROOT / request.subject_paper_id / 'references',
                    tmp_path / request.subject_paper_id / 'references')
    store = ReferenceStore(tmp_path)
    tool = ReviewerReaderTool(ReferenceArtifactReaderTool(store))
    _, path, _ = store.verify_artifact_file(request.subject_paper_id, BODY_ID)
    return request, tool, path


@pytest.mark.parametrize('mutation,code', [('missing','reader.file.missing'), ('hash','reader.artifact.hash_mismatch')])
def test_authorized_unreadable_artifact_is_not_outside_scope(tmp_path, mutation, code):
    request, tool, path = local_tool(tmp_path)
    if mutation == 'missing':
        path.unlink()
    else:
        path.write_text('changed', encoding='utf-8')
    result = asyncio.run(tool.ainvoke(ReaderArguments(artifact_id=BODY_ID, max_chars=10), scope=request))
    assert not result.succeeded
    assert result.payload['execution_issues'][0]['code'] == code
    assert 'outside reviewer scope' not in (result.error or '')
    with pytest.raises(PermissionError):
        asyncio.run(tool.ainvoke(ReaderArguments(artifact_id='outside', max_chars=10), scope=request))


def test_partial_batch_failure_preserves_good_read_and_classifies_insufficiency(tmp_path):
    request, tool, path = local_tool(tmp_path)
    path.unlink()
    good_id = request.evidence[0].artifact_id
    client = ScriptedClient(ModelResponse(content=None, tool_calls=(ModelToolCall(
        id='batch', name='reader', arguments={'reads':[{'artifact_id':good_id,'max_chars':30},
                                                     {'artifact_id':BODY_ID,'max_chars':30}]}),)),
        ModelResponse(content=json.dumps({'novelty_point_id':'NP-3','status':'insufficient_evidence',
                                         'supplement_request':{'reason':'remaining comparison unavailable'}})))
    result = asyncio.run(NoveltyEvidenceReviewer(client,
        tool_registry=ResearcherToolRegistry([tool])).review_card(request))
    assert result.incomplete_reason == 'material_unavailable'
    assert result.execution_issues[0].code == 'reader.file.missing'
    assert len(result.reader_observations) == 1


@pytest.mark.parametrize('code,expected', [('CONTEXT_LIMIT_EXCEEDED','model.context_limit'),
                                         ('CONTEXT_MEASUREMENT_UNAVAILABLE','model.context_unavailable')])
def test_reviewer_records_precise_context_failure(code, expected):
    from backend.env import ModelContextAdmissionError
    class FailingClient:
        async def acomplete(self, messages, *, options=None):
            raise ModelContextAdmissionError({'code':code})
    result = asyncio.run(_reviewer(FailingClient(), RecordingReader()).review_card(_request()))
    assert result.incomplete_reason == 'technical_error'
    assert result.execution_issues[0].code == expected
    assert result.verdict is None


def test_bound_artifact_with_missing_manifest_is_material_unavailable(tmp_path):
    request, tool, _ = local_tool(tmp_path)
    (tmp_path/request.subject_paper_id/'references'/'list.json').unlink()
    result = asyncio.run(tool.ainvoke(ReaderArguments(artifact_id=request.evidence[0].artifact_id),scope=request))
    assert result.payload['execution_issues'][0]['code'] == 'reader.artifact.unregistered'
