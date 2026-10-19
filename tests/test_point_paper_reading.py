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
        if self.calls==1:
            output={'required_segments':[last],'reason':'Read the indexed late contribution segment.'}
        elif self.calls==2:
            assert 'LATE-OMEGA' in messages[-1].content
            output={'novelty_points':[{'point_id':str(i),'claim':f'LATE-OMEGA mechanism {i}',
                'technical_features':[str(i)],'source_locations':[location]} for i in range(3)]}
        else:
            output={'deletions':[]}
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
        if len(self.calls) == 1:
            assert '是否需要阅读全文' in text
            assert 'PaperInput.full_text' in text
            data = {'needs_full_text': False, 'reason': 'Read the indexed late contribution segment.'}
        elif len(self.calls) == 2:
            data = {'segment_id': self.segment}
        elif len(self.calls) == 3:
            assert 'LATE-OMEGA' in text
            data = {'finish': True}
        elif len(self.calls) == 4:
            # This assertion proves the generation request contains the read body,
            # not merely a menu or a scripted claim unrelated to delivered material.
            assert 'LATE-OMEGA' in text and self.location in text
            data = {'novelty_points': [{'point_id':str(i), 'claim': f'LATE-OMEGA mechanism {i}',
                'technical_features': [f'mechanism {i}'], 'source_locations':[self.location]} for i in range(3)]}
        else:
            data = {'deletions': []}
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
    assert reading['scope_decision']['needs_full_text'] is False
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
    with pytest.raises(PaperReadingError, match=code):
        agent.extract(digest, previous_brief=None, attempt=1)
    assert agent.last_trace['reading']['status'] == 'failed'
    assert agent.last_trace['reading']['events'][-1]['code'] == code
    assert agent.last_trace['calls'] == []


def test_invalid_selection_gets_one_local_repair(tmp_path):
    _, ref, digest = prepare(tmp_path)
    client = TailClient(ref)
    original = client.complete
    count = 0
    def complete(messages, *, options=None):
        nonlocal count
        count += 1
        if count == 2:
            return ModelResponse(content='{"segment_id":"invented"}')
        return original(messages, options=options)
    client.complete = complete
    agent = NoveltyPointExtractorAgent(client)
    agent.extract(digest, previous_brief=None, attempt=1)
    assert agent.last_trace['reading']['events'][0]['code'] == 'tool.arguments.schema_invalid'


def test_unknown_citation_rejected_and_recorded(tmp_path):
    _, ref, digest = prepare(tmp_path)
    client = TailClient(ref)
    original = client.complete
    def complete(messages, *, options=None):
        if len(client.calls) >= 3:
            client.calls.append(messages[-1].content)
            return ModelResponse(content=json.dumps({'novelty_points':[
                {'point_id':'bad', 'claim':'unsupported', 'source_locations':['invented-range']}]}))
        return original(messages, options=options)
    client.complete = complete
    agent = NoveltyPointExtractorAgent(client)
    with pytest.raises(PaperReadingError, match='model.output_reference'):
        agent.extract(digest, previous_brief=None, attempt=1)
    assert len(agent.last_trace['failures']) == 4
    assert all(item['code'] == 'model.output_reference' for item in agent.last_trace['failures'])


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
    assert trace['reading']['status'] == 'failed'
    assert trace['reading']['generation_allowed'] is False
    assert len(trace['calls']) == 2
    assert not (tmp_path / paper().paper_id / 'novelty-points.json').exists()


def test_read_budget_stops_with_visible_coverage_gap(tmp_path, monkeypatch):
    import novelty_agent_framework.agents.point_extractor as module
    monkeypatch.setattr(module, 'MAX_READING_CALLS', 2)
    _, ref, digest = prepare(tmp_path)
    client = TailClient(ref)
    original = client.complete
    def complete(messages, *, options=None):
        # No finish decision after budget exhaustion; jump directly to generation.
        if len(client.calls) == 2:
            client.calls.append('skipped finish')
        return original(messages, options=options)
    client.complete = complete
    agent = NoveltyPointExtractorAgent(client)
    agent.extract(digest, previous_brief=None, attempt=1)
    reading = agent.last_trace['reading']
    assert reading['status'] == 'budget_exhausted'
    assert reading['events'][-1]['code'] == 'harness.budget.turns_exhausted'
    assert agent.last_trace['coverage_complete'] is False


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


class ScopeClient:
    """Observe that generation only happens after the requested reads are delivered."""
    def __init__(self, ref, decision, *, premature_finish=False):
        self.reader = PaperReader(ref)
        self.decision = decision
        self.premature_finish = premature_finish
        self.calls = []
        self.generated = False

    def complete(self, messages, *, options=None):
        text = messages[-1].content
        self.calls.append(text)
        if '"phase": "decide_scope"' in text:
            assert '是否需要阅读全文' in text
            result = self.decision
        elif '"phase": "read_segments"' in text:
            payload = json.loads(text[text.index('{"phase"'):])
            if self.premature_finish and payload['read_segments']:
                self.premature_finish = False
                result = {'finish': True}
            else:
                result = {'segment_id': payload['available_segments'][0]['segment_id']}
        elif not self.generated:
            requested = self.decision.get('required_segments') or [
                row['segment_id'] for row in self.reader.load()[1]['segments']]
            visible_reads = json.loads(text.split(
                '已核验的论文阅读记录（source_locations 只能使用下列 source_location）：\n'
            )[1])['reads']
            for segment, visible in zip(requested, visible_reads, strict=True):
                chunk = self.reader.read(segment)
                assert visible == chunk
            self.generated = True
            location = self.reader.read(requested[-1])['source_location']
            result = {'novelty_points': [
                {'point_id': str(i), 'claim': f'LATE-OMEGA mechanism {i}',
                 'source_locations': [location]} for i in range(3)]}
        else:
            result = {'deletions': []}
        return ModelResponse(content=json.dumps(result))


@pytest.mark.parametrize('use_prompts', [False, True])
@pytest.mark.parametrize('scope', ['full', 'segments'])
def test_scope_gate_delivers_required_reads_before_generation(tmp_path, scope, use_prompts):
    _, ref, digest = prepare(tmp_path)
    decision = ({'needs_full_text': True, 'reason': 'Check every section before extraction.'}
                if scope == 'full' else
                {'required_segments': ['body-4', 'body-2'], 'reason': 'Check late contribution and methods.'})
    client = ScopeClient(ref, decision, premature_finish=scope == 'full')
    agent = NoveltyPointExtractorAgent(client, prompts=(
        PromptLibrary(Path('backend/src/novelty_agent_framework/prompts')) if use_prompts else None))
    points = agent.extract(digest, previous_brief=None, attempt=1)
    trace = agent.last_trace['reading']
    assert points and trace['scope_decision'] == decision
    assert trace['generation_allowed'] is True
    if scope == 'full':
        assert not trace['unread_segments']
        assert trace['status'] == 'all_segments_read'
        assert trace['events'][0]['action'] == 'repair_arguments'
    else:
        assert [row['segment_id'] for row in trace['reads']] == ['body-4', 'body-2']
        assert trace['unread_segments'] == ['body-1', 'body-3']
        assert trace['status'] == 'requested_segments_read'
        assert trace['model_calls_used'] == 1


@pytest.mark.parametrize('decision', [
    {'novelty_points': []}, {'segment_id': 'body-4'},
    {'needs_full_text': 'false', 'reason': 'Invalid boolean'},
    {'needs_full_text': False, 'reason': ' '},
    {'required_segments': [], 'reason': 'Empty'},
    {'required_segments': ['missing'], 'reason': 'Unknown'},
    {'required_segments': ['body-4', 'body-4'], 'reason': 'Duplicate'},
    {'required_segments': [{}], 'reason': 'Invalid type'},
])
def test_invalid_scope_cannot_bypass_gate(tmp_path, decision):
    _, ref, digest = prepare(tmp_path)
    client = ScopeClient(ref, decision)
    agent = NoveltyPointExtractorAgent(client)
    with pytest.raises(PaperReadingError, match='tool.arguments.schema_invalid'):
        agent.extract(digest, previous_brief=None, attempt=1)
    assert len(client.calls) == 2  # one local correction, then fail closed
    assert agent.last_trace['reading']['generation_allowed'] is False
    assert not agent.last_trace.get('generation')


@pytest.mark.parametrize('decision', [
    {'needs_full_text': True, 'reason': 'Need all sections'},
    {'required_segments': ['body-4', 'body-2'], 'reason': 'Need two sections'},
])
def test_unfinished_required_reads_block_generation(tmp_path, monkeypatch, decision):
    import novelty_agent_framework.agents.point_extractor as module
    monkeypatch.setattr(module, 'MAX_READING_CALLS', 2)
    _, ref, digest = prepare(tmp_path)
    agent = NoveltyPointExtractorAgent(ScopeClient(ref, decision))
    with pytest.raises(PaperReadingError, match='harness.budget.turns_exhausted'):
        agent.extract(digest, previous_brief=None, attempt=1)
    trace = agent.last_trace['reading']
    assert trace['reads'] and trace['unread_segments']
    assert trace['generation_allowed'] is False
    assert not agent.last_trace.get('generation')
