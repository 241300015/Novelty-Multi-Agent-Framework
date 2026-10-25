"""Issue #20: persistent reading, cross-process recovery, and actual model visibility."""
import asyncio
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from backend.env import ModelResponse, PromptLibrary
from novelty_agent_framework.agents.point_extractor import NoveltyPointExtractorAgent, build_paper_digest
from novelty_agent_framework.core.extractor_harness import ExtractorHarnessError
from novelty_agent_framework.core.paper_reading import (
    PaperReader, PaperReadingError, latest_reading_reference,
)
from novelty_agent_framework.persistence import persist_paper_input, persist_workflow_input
from novelty_agent_framework.schemas import PaperDocument, PaperInput


def paper():
    return PaperInput(paper_id='long-paper', title='Long paper', abstract='A short overview',
        full_text='Background without contributions.\n' * 400 + '\nWe propose the LATE-OMEGA independent scheduling mechanism.')


def prepare(tmp_path):
    value = paper()
    workspace, ref = persist_workflow_input(value, output_root=tmp_path, with_reading_reference=True)
    return workspace, ref, build_paper_digest(value).model_copy(update={'paper_source': ref})


@pytest.mark.parametrize('entry', ['direct', 'parsed_pdf'])
def test_fresh_process_reads_entire_input(tmp_path, entry):
    value = paper()
    if entry == 'parsed_pdf':
        import pymupdf
        from novelty_agent_framework.processing import DefaultPaperProcessor
        pdf_path = tmp_path / 'long-paper.pdf'
        with pymupdf.open() as pdf:
            for i in range(8):
                page = pdf.new_page()
                page.insert_text((72, 72), (''.join(f'Section {i} detail {j} explains the method.\n' for j in range(35)))
                    + ('We propose LATE-OMEGA scheduling.' if i == 7 else 'Continued.'))
            pdf.save(pdf_path)
        processor = DefaultPaperProcessor(parser='text_layer')
        document = processor.process(pdf_path)
        value = processor.to_paper_input(document)
        assert len(value.full_text) > 2000
        workspace = persist_paper_input(document, value, output_root=tmp_path)
        pdf_path.unlink()  # Recovery must need only PaperInput, not the original PDF.
    else:
        workspace = persist_workflow_input(value, output_root=tmp_path)
    # Child only receives a persisted directory; no serialized body or parent objects.
    script = '''
import json,sys
from pathlib import Path
from novelty_agent_framework.core.paper_reading import PaperReader,latest_reading_reference
from novelty_agent_framework.processing import DefaultPaperProcessor
def reject_pdf_processing(*args, **kwargs):
    raise AssertionError('Persisted PaperInput must not trigger PDF processing')
DefaultPaperProcessor.process=reject_pdf_processing
r=PaperReader(latest_reading_reference(Path(sys.argv[1])))
p,index=r.load()
body=''.join(r.read(row['segment_id'])['text'] for row in index['segments'])
assert body==p.full_text and 'LATE-OMEGA' in body
from backend.env import ModelResponse
from novelty_agent_framework.agents.point_extractor import NoveltyPointExtractorAgent,build_paper_digest
last=index['segments'][-1]['segment_id']
location=r.read(last)['source_location']
class Model:
    calls=0
    def complete(self,messages,*,options=None):
        self.calls+=1
        text=messages[-1].content
        ctx=json.loads(text[text.index('{'):])
        if ctx['phase']=='reading_scope':
            option=next(o for o in ctx['options'] if o.get('segment_id')==last)
            output={'option_ids':[option['option_id']],'reason':'Read the late method segment.'}
        elif ctx['phase']=='generate':
            assert 'LATE-OMEGA' in ctx['reads'][0]['text']
            output={'option_id':ctx['option_id'],'points':[{'claim':'LATE-OMEGA scheduling',
                'technical_features':['scheduling'],'source_locations':[location]}]}
        elif ctx['phase']=='post_generation_reading':
            option=next(o for o in ctx['options'] if o['kind']=='continue_review')
            output={'option_ids':[option['option_id']],'reason':'The only explicit method is already read; proceed to coverage review.'}
        else:
            output={'point_reviews':[{'point_id':p['point_id'],'status':'pass','issues':[]}
                for p in ctx['points']],'missing_contributions':[]}

        return ModelResponse(content=json.dumps(output))
agent=NoveltyPointExtractorAgent(Model())
digest=build_paper_digest(p).model_copy(update={'paper_source':r.reference})
assert 'LATE-OMEGA' not in digest.full_text_excerpt
points=agent.extract(digest,previous_brief=None,attempt=1)
assert points[0].source_locations==[location]
assert agent.last_trace['reading']['reads'][0]['start'] > 2000
assert agent.last_trace['reading']['generation_allowed'] is True
print(json.dumps({'chars':len(body), 'segments':len(index['segments']), 'points':len(points)}))
'''
    env = dict(os.environ, PYTHONPATH=os.pathsep.join([str(Path('backend/src').resolve()), str(Path.cwd())]))
    result = subprocess.run([sys.executable, '-c', script, str(workspace)], env=env,
        text=True, capture_output=True, check=True)
    assert json.loads(result.stdout)['chars'] == len(value.full_text)


def test_new_input_refreshes_body_and_preserves_old_snapshot(tmp_path):
    workspace, old, _ = prepare(tmp_path)
    changed = paper().model_copy(update={'full_text': 'new body'})
    _, new = persist_workflow_input(changed, output_root=tmp_path, with_reading_reference=True)
    assert (workspace / 'paper-input/full.md').read_text().strip() == 'new body'
    assert PaperReader(old).load()[0].full_text == paper().full_text
    assert PaperReader(new).load()[0].full_text == 'new body'
    assert old.run_id != new.run_id


class TailClient:
    def __init__(self, ref):
        self.reader = PaperReader(ref)
        _, index = self.reader.load()
        self.segment = index['segments'][-1]['segment_id']
        self.location = self.reader.read(self.segment)['source_location']
        self.calls = []

    def complete(self, messages, *, options=None):
        text = messages[-1].content
        self.calls.append(text)
        ctx = json.loads(text[text.index('{'):])
        if ctx['phase'] == 'reading_scope':
            assert ctx['digest']['paper_source']
            assert any(row['segment_id'] == self.segment for row in ctx['content_list'])
            option = next(o for o in ctx['options'] if o.get('segment_id') == self.segment)
            data = {'option_ids':[option['option_id']], 'reason':'Read the late contribution.'}
        elif ctx['phase'] == 'generate':
            assert 'LATE-OMEGA' in ctx['reads'][0]['text'] and self.location in text
            data = {'option_id':ctx['option_id'], 'points':[{'claim':'LATE-OMEGA mechanism',
                'technical_features':['event scheduling'], 'source_locations':[self.location]}]}
        elif ctx['phase'] == 'post_generation_reading':
            option = next(o for o in ctx['options'] if o['kind'] == 'continue_review')
            data = {'option_ids':[option['option_id']], 'reason':'Review the read contribution.'}
        else:
            data = {'point_reviews':[{'point_id':p['point_id'], 'status':'pass', 'issues':[]} for p in ctx['points']],
                'missing_contributions':[], 'comparisons':[]}
        return ModelResponse(content=json.dumps(data))


@pytest.mark.parametrize('use_prompts', [False, True])
def test_extractor_reads_tail_and_saves_verified_locations(tmp_path, use_prompts):
    _, ref, digest = prepare(tmp_path)
    assert 'LATE-OMEGA' not in digest.full_text_excerpt
    agent = NoveltyPointExtractorAgent(TailClient(ref), prompts=(
        PromptLibrary(Path('backend/src/novelty_agent_framework/prompts')) if use_prompts else None))
    points = agent.extract(digest, previous_brief=None, attempt=1)
    reading = agent.last_trace['reading']
    assert reading['reads'][0]['start'] > 2000
    assert all(p.source_locations == [reading['reads'][0]['source_location']] for p in points)
    assert reading['unread_segments']
    assert reading['scope_decision']['option_ids']
    assert reading['generation_allowed'] is True
    assert agent.last_trace['coverage_complete'] is False


@pytest.mark.parametrize('damage,code', [
    ('missing', 'reader.file.missing'), ('hash', 'reader.artifact.hash_mismatch'),
    ('run', 'reader.manifest.invalid'), ('version', 'reader.manifest.invalid'),
    ('escape', 'reader.artifact.path_escape'), ('digest', 'recovery.scope.changed'),
])
def test_failures_have_codes_and_no_model_read_fallback(tmp_path, damage, code):
    workspace, ref, digest = prepare(tmp_path)
    index_path = workspace / ref.index_path
    if damage == 'missing':
        (index_path.parent / 'paper.json').unlink()
    elif damage == 'hash':
        (index_path.parent / 'paper.json').write_text('{}')
    elif damage == 'run':
        ref = ref.model_copy(update={'run_id':'different-run'})
    elif damage in {'version', 'escape'}:
        index = json.loads(index_path.read_text())
        if damage == 'version':
            index['schema_version'] = 99
        else:
            index['artifacts'][0]['relative_path'] = '../paper.json'
        index_path.write_text(json.dumps(index))
        ref = ref.model_copy(update={'sha256':hashlib.sha256(index_path.read_bytes()).hexdigest()})
    else:
        digest = digest.model_copy(update={'title': 'wrong paper'})
    digest = digest.model_copy(update={'paper_source':ref})
    agent = NoveltyPointExtractorAgent()
    with pytest.raises((PaperReadingError, ExtractorHarnessError), match=code):
        agent.extract(digest, previous_brief=None, attempt=1)
    assert agent.last_trace['execution_status'] == 'failed'
    assert agent.last_trace['events'][-1]['code'] == code
    assert agent.last_trace['calls'] == []


def test_invalid_selection_gets_one_local_repair(tmp_path):
    _, ref, digest = prepare(tmp_path)
    client = TailClient(ref)
    original = client.complete
    count = 0
    def complete(messages, *, options=None):
        nonlocal count
        count += 1
        if count == 1:
            return ModelResponse(content='{"option_ids":["invented"],"reason":"invalid"}')
        return original(messages, options=options)
    client.complete = complete
    agent = NoveltyPointExtractorAgent(client)
    agent.extract(digest, previous_brief=None, attempt=1)
    assert agent.last_trace['events'][0]['code'] == 'tool.arguments.schema_invalid'


def test_unknown_citation_rejected_and_recorded(tmp_path):
    _, ref, digest = prepare(tmp_path)
    client = TailClient(ref)
    original = client.complete
    def complete(messages, *, options=None):
        text = messages[-1].content
        ctx = json.loads(text[text.index('{'):])
        if ctx['phase'] == 'generate':
            return ModelResponse(content=json.dumps({'option_id':ctx['option_id'], 'points':[
                {'claim':'unsupported','technical_features':['invented'], 'source_locations':['invented-range']}]}))
        return original(messages, options=options)
    client.complete = complete
    agent = NoveltyPointExtractorAgent(client)
    with pytest.raises(ExtractorHarnessError, match='model.output_reference'):
        agent.extract(digest, previous_brief=None, attempt=1)
    assert agent.last_trace['execution_status'] == 'failed'
    assert all(item['code'] == 'model.output_reference' for item in agent.last_trace['events'])


def test_runtime_manifest_links_frozen_index(tmp_path):
    from novelty_agent_framework.core.runtime_artifacts import RuntimeArtifactManager, RuntimeDebugConfig
    manager = RuntimeArtifactManager('long-paper', config=RuntimeDebugConfig(output_root=tmp_path))
    manager.activate()
    try:
        _, ref, _ = prepare(tmp_path)
        assert ref.run_id == manager.run_id
        manifest = json.loads((manager.run_dir / 'manifest.json').read_text())
        assert manifest['paper_reading_indexes'][-1]['sha256'] == ref.sha256
    finally:
        manager.deactivate()


def test_workflow_persists_success_trace_without_runtime_debug(tmp_path):
    from novelty_agent_framework.workflows import NoveltyWorkflow
    workflow = NoveltyWorkflow.default(output_root=tmp_path)
    class PersistedClient:
        def complete(self, messages, *, options=None):
            # The active index is discovered from disk, even in this fake model.
            ref = latest_reading_reference(tmp_path / paper().paper_id)
            if not hasattr(self, 'client'):
                self.client = TailClient(ref)
            return self.client.complete(messages, options=options)
    workflow.point_extractor = NoveltyPointExtractorAgent(PersistedClient())
    result = asyncio.run(workflow._extract_points({'paper':paper()}))
    workspace = tmp_path / paper().paper_id
    trace = json.loads((workspace / 'point_extraction_trace.json').read_text())
    assert trace['reading']['scope_decision']['reason']
    assert trace['reading']['generation_allowed'] is True
    ref = latest_reading_reference(workspace)
    assert (workspace / ref.index_path).with_name('point_extraction_trace.json').is_file()
    persisted = json.loads((workspace / 'novelty-points.json').read_text())
    assert persisted['novelty_points'][0]['source_locations'] == result['novelty_points'][0].source_locations
    assert trace['reading']['reads'][0]['end'] == len(paper().full_text)


def test_workflow_persists_failure_trace_without_runtime_debug(tmp_path):
    from novelty_agent_framework.workflows import NoveltyWorkflow
    from novelty_agent_framework.core.errors import WorkflowExecutionError
    workflow = NoveltyWorkflow.default(output_root=tmp_path)
    class BrokenClient:
        def complete(self, messages, *, options=None):
            return ModelResponse(content='not json')
    workflow.point_extractor = NoveltyPointExtractorAgent(BrokenClient())
    with pytest.raises(WorkflowExecutionError):
        asyncio.run(workflow._extract_points({'paper':paper()}))
    trace = json.loads((tmp_path / paper().paper_id / 'point_extraction_trace.json').read_text())
    assert trace['execution_status'] == 'failed'
    assert trace['reading']['generation_allowed'] is False
    assert len(trace['calls']) == 2
    assert not (tmp_path / paper().paper_id / 'novelty-points.json').exists()


def test_no_readable_segment_blocks_generation(tmp_path):
    _, ref, digest = prepare(tmp_path)
    client = TailClient(ref)
    agent = NoveltyPointExtractorAgent(client, harness_config={'max_read_chars':1})
    with pytest.raises(ExtractorHarnessError, match='no readable segment'):
        agent.extract(digest, previous_brief=None, attempt=1)
    assert client.calls == []
    assert agent.last_trace['reading']['generation_allowed'] is False


def test_workflow_records_failure_before_agent_starts(tmp_path, monkeypatch):
    from novelty_agent_framework.workflows import NoveltyWorkflow
    from novelty_agent_framework.core.errors import WorkflowExecutionError
    from novelty_agent_framework.schemas.failures import FailureCode
    def missing(self):
        raise PaperReadingError(FailureCode.READER_FILE_MISSING, 'lost input snapshot')
    monkeypatch.setattr(PaperReader, 'load', missing)
    workflow = NoveltyWorkflow.default(output_root=tmp_path)
    with pytest.raises(WorkflowExecutionError):
        asyncio.run(workflow._extract_points({'paper':paper()}))
    trace = json.loads((tmp_path / paper().paper_id / 'point_extraction_trace.json').read_text())
    assert trace['reading']['events'][0]['code'] == 'reader.file.missing'
    assert trace['reading']['events'][0]['node'] == 'extract_points.load'


@pytest.mark.parametrize('use_prompts', [False, True])
@pytest.mark.parametrize('scope', ['full', 'segment'])
def test_scope_gate_delivers_selected_reads_before_generation(tmp_path, scope, use_prompts):
    from test_extractor_controlled import Client, baseline, read
    _, ref, digest = prepare(tmp_path)
    reader = PaperReader(ref)
    def handler(ctx):
        if ctx['phase'] == 'reading_scope':
            return read(ctx, all=scope == 'full')
        if ctx['phase'] == 'generate':
            expected = reader.load()[1]['segments'] if scope == 'full' else [reader.load()[1]['segments'][-1]]
            assert ctx['reads'] == [reader.read(r['segment_id']) for r in expected]
        return baseline(ctx)
    agent = NoveltyPointExtractorAgent(Client(handler), prompts=(
        PromptLibrary(Path('backend/src/novelty_agent_framework/prompts')) if use_prompts else None))
    assert agent.extract(digest, previous_brief=None, attempt=1)
    assert bool(agent.last_trace['reading']['unread_segments']) == (scope != 'full')
    assert agent.last_trace['reading']['generation_allowed'] is True


@pytest.mark.parametrize('decision', [
    {'novelty_points': []}, {'segment_id': 'body-4'},
    {'needs_full_text': True, 'reason': 'removed protocol'},
    {'option_ids': [], 'reason': 'empty'},
    {'option_ids': ['missing'], 'reason': 'unknown'},
    {'option_ids': [{}], 'reason': 'invalid type'},
])
def test_invalid_scope_cannot_bypass_gate(tmp_path, decision):
    from test_extractor_controlled import Client
    _, _, digest = prepare(tmp_path)
    client = Client(lambda ctx: decision)
    agent = NoveltyPointExtractorAgent(client)
    with pytest.raises(ExtractorHarnessError):
        agent.extract(digest, previous_brief=None, attempt=1)
    assert len(client.calls) == 2
    assert agent.last_trace['reading']['generation_allowed'] is False
    assert not agent.last_trace['versions']
