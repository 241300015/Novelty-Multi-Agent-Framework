"""A profile must select the templates actually sent to each role's model."""
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from backend.env import ModelResponse, PromptLibrary
from novelty_agent_framework.agents import build_paper_digest
from novelty_agent_framework.config import build_model_registry, build_workflow, load_application_config
from novelty_agent_framework.config.experiment import freeze_config, preflight_config
from novelty_agent_framework.schemas import PaperInput, NoveltyPointReview, InsufficientFinalEvidence

ROOT = Path('backend/src/novelty_agent_framework/prompts')


class Client:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.messages = []

    def complete(self, messages, *, options=None):
        self.messages.append(messages)
        if options and options.response_format and options.response_format.get('json_schema', {}).get('name') == 'harness_decision':
            choices = options.response_format['json_schema']['schema']['properties']['option_id']['enum']
            return ModelResponse(content=json.dumps({'option_id': choices[-1]}))
        return ModelResponse(content=json.dumps(next(self.responses)))


def test_profile_names_reach_all_extractor_and_coordinator_model_requests(tmp_path, monkeypatch):
    mapping = {
        'read': 'extractor/reader_choices', 'generate': 'extractor/generate',
        'semantic_review': 'extractor/semantic_review', 'correct': 'extractor/correct',
        'supplement': 'coordinator/supplement', 'synthesize': 'coordinator/synthesize',
    }
    for operation, source in mapping.items():
        (tmp_path / f'{operation}.md').write_text((ROOT / f'{source}.md').read_text() + f'\nCUSTOM_{operation}\n')
    monkeypatch.setattr('novelty_agent_framework.config.factory.build_prompt_library', lambda *args: PromptLibrary(tmp_path))
    monkeypatch.setattr('novelty_agent_framework.config.experiment.PROMPTS_ROOT', tmp_path)
    config = load_application_config(environ={}, overrides={
        'point_extractor': {'harness': {'reader_prompt':'read', 'generate_prompt':'generate',
            'review_prompt':'semantic_review', 'correction_prompt':'correct'}},
        'coordinator': {'prompt_names': {k:k for k in ('supplement','synthesize')}},
    })
    workflow = build_workflow(config, output_root=tmp_path / 'outputs')
    from novelty_agent_framework.persistence import persist_workflow_input
    from test_extractor_controlled import Client as ExtractorClient, baseline, point, rejected, dedup_choice
    first_read = True
    def handler(ctx):
        nonlocal first_read
        if ctx['phase'] == 'reading_scope' and first_read:
            first_read = False
            return 'not json'
        if ctx['phase'] == 'generate':
            return {'option_id':ctx['option_id'], 'points':[point(ctx), point(ctx, '调度机制的改写')]}
        if ctx['phase'] == 'semantic_review' and len(ctx['points']) == 2:
            return rejected(ctx)
        if ctx['phase'] == 'deduplicate':
            return dedup_choice(ctx)
        return baseline(ctx)
    extractor_client = ExtractorClient(handler)
    extractor = workflow.services.point_extractor
    extractor.model_client = extractor_client
    paper = PaperInput(paper_id='prompt-profile', title='example', full_text='source', abstract='source')
    _, ref = persist_workflow_input(paper, output_root=tmp_path, with_reading_reference=True)
    extracted = extractor.extract(build_paper_digest(paper).model_copy(update={'paper_source':ref}), previous_brief=None, attempt=1)
    for record, messages in zip(extractor.last_trace['calls'], extractor_client.messages, strict=True):
        phase = record['input']['phase']
        expected = 'read' if phase == 'reading_scope' else 'generate' if phase in {'generate','deduplicate'} else 'semantic_review'
        assert record['prompt_name'] == expected
        assert f'CUSTOM_{expected}' in messages[1].content
    assert 'CUSTOM_correct' in extractor_client.calls[1]['correction']['procedure']
    assert len(extractor_client.calls) == 6
    coordinator = workflow.services.coordinator
    brief = coordinator.plan(paper, points=extracted, attempt=1)
    draft = {'conclusions':[{'novelty_point_id':p.point_id,'summary':'核验未完成','supporting_card_ids':[],'counter_card_ids':[]} for p in extracted], 'limitations':[]}
    supplement_tasks = [dict(task_id='model-task', novelty_point_id=extracted[0].point_id,
                             task_type='research_gap', language='en', description='Expand evidence coverage')]
    coordinator_client = Client([{"trigger_code": "evidence.missing", "decisions": [{"point_id": extracted[0].point_id,
        "action": "research_gap", "reason": "Expand uncovered evidence dimensions",
        "research_tasks": supplement_tasks}]},draft])
    coordinator.model_client = coordinator_client
    coordinator.plan_supplement(paper, brief=brief, existing_evidence=[],
        insufficient_final_evidence_points=[InsufficientFinalEvidence(
            novelty_point_id=extracted[0].point_id, valid_card_count=0, required_card_count=1)], attempt=2)
    reviews = [NoveltyPointReview(novelty_point_id=p.point_id,status='insufficient_evidence',incomplete_reason='material_unavailable') for p in extracted]
    coordinator.synthesize(paper,brief=brief,evidence=[],novelty_reviews=reviews,rejected_evidence=[],insufficient_final_evidence_points=[])
    assert 'CUSTOM_supplement' in coordinator_client.messages[0][1].content
    assert 'CUSTOM_supplement' in coordinator_client.messages[1][1].content
    assert 'CUSTOM_synthesize' in coordinator_client.messages[2][1].content
    frozen = freeze_config(config)
    for role in ('point_extractor','coordinator'):
        for name in frozen['selected_prompts'][role].values():
            assert name['exists'] and len(name['sha256']) == 64


def test_legacy_custom_prompt_list_cannot_silently_select_default():
    config = load_application_config(environ={}, overrides={'coordinator': {'prompts':['coordinator/custom']}})
    assert 'legacy_prompt_selection_ignored' in {i['code'] for i in preflight_config(config, environ={'LOCAL_VLLM_API_KEY':'local'})}


def test_missing_named_prompt_is_rejected_before_execution():
    config = load_application_config(environ={}, overrides={'point_extractor': {'harness': {'review_prompt':'missing'}}})
    errors = [i for i in preflight_config(config, environ={'LOCAL_VLLM_API_KEY':'local'}) if i['code']=='prompt_missing']
    assert [i['path'] for i in errors] == ['point_extractor.semantic_review']


def test_context_profile_reaches_actual_client_and_safe_snapshot():
    config = load_application_config(environ={}, overrides={'models': {'local-qwen2.5-7b': {'context_admission': {'mode':'enforce','counter':'vllm','vllm_tools_mode':'v0_8_5_kwargs'}}}})
    profile = build_model_registry(config).client_for('local-qwen2.5-7b').profile
    assert profile.context_admission.mode == 'enforce'
    assert profile.context_admission.on_unavailable == 'reject'
    assert profile.context_admission.vllm_tools_mode == 'v0_8_5_kwargs'
    assert freeze_config(config)['effective_config']['models']['local-qwen2.5-7b']['context_admission']['counter'] == 'vllm'


@pytest.mark.parametrize('patch',[{'vllm_tools_mode':'automatic'}, {'timeout_seconds':61},{'tokenize_path':'https://example.org/tokenize'}, {'tokenize_path':'//example.org/tokenize'}])
def test_invalid_context_transport_config_fails_fast(patch):
    with pytest.raises(ValidationError):
        load_application_config(environ={}, overrides={'models': {'local-qwen2.5-7b': {'context_admission':patch}}})


@pytest.mark.parametrize('patch', [
    {'harness': {'enabled':False}}, {'conservative_dedup':True},
    {'prompt_names':{'read':'extractor/read_paper'}}, {'prompts':['extractor/read_paper']},
])
def test_obsolete_extractor_config_is_rejected(patch):
    with pytest.raises(ValidationError):
        load_application_config(environ={}, overrides={'point_extractor':patch})


def test_projected_dictionary_config_rejects_obsolete_extractor_settings(tmp_path):
    from novelty_agent_framework.config.loader import legacy_shape
    config = legacy_shape(load_application_config(environ={}))
    config['agents']['point_extractor']['prompt_names'] = {'read':'removed'}
    with pytest.raises(ValueError, match='Obsolete point_extractor settings'):
        build_workflow(config, output_root=tmp_path)
