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
        if options and options.response_format and options.response_format.get('type') == 'json_schema':
            choices = options.response_format['json_schema']['schema']['properties']['option_id']['enum']
            return ModelResponse(content=json.dumps({'option_id': choices[-1]}))
        return ModelResponse(content=json.dumps(next(self.responses)))


def test_profile_names_reach_all_extractor_and_coordinator_model_requests(tmp_path, monkeypatch):
    mapping = {
        'read': 'extractor/read_paper',
        'generate': 'extractor/extract_points', 'coverage': 'extractor/extract_points',
        'deduplicate': 'reviewer/review_points',
        'supplement': 'coordinator/supplement', 'synthesize': 'coordinator/synthesize',
    }
    for operation, source in mapping.items():
        (tmp_path / f'{operation}.md').write_text((ROOT / f'{source}.md').read_text() + f'\nCUSTOM_{operation}\n')
    monkeypatch.setattr('novelty_agent_framework.config.factory.build_prompt_library', lambda *args: PromptLibrary(tmp_path))
    monkeypatch.setattr('novelty_agent_framework.config.experiment.PROMPTS_ROOT', tmp_path)
    config = load_application_config(environ={}, overrides={
        'point_extractor': {'harness': {'enabled': False}, 'conservative_dedup': False, 'prompt_names': {k:k for k in ('read','generate','deduplicate','coverage')}},
        'coordinator': {'prompt_names': {k:k for k in ('supplement','synthesize')}},
    })
    workflow = build_workflow(config, output_root=tmp_path / 'outputs')
    points = [{'point_id':f'P{i}', 'claim':f'独立机制{i}', 'technical_features':[f'机制{i}'], 'source_locations':['abstract']} for i in range(3)]
    extractor_client = Client([{'novelty_points':points},
        {'deletions':[{'index':3,'duplicate_of':1,'reason':'等价复述'}]}, {'novelty_points':[]}])
    extractor = workflow.services.point_extractor
    extractor.model_client = extractor_client
    paper = PaperInput(paper_id='prompt-profile', title='example', full_text='source', abstract='source')
    extracted = extractor.extract(build_paper_digest(paper), previous_brief=None, attempt=1)
    for messages, operation in zip(extractor_client.messages, ('generate','deduplicate','coverage')):
        assert f'CUSTOM_{operation}' in messages[1].content
    assert len(extractor_client.messages) == 3
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
    config = load_application_config(environ={}, overrides={'point_extractor': {'harness': {'enabled': False}, 'prompt_names': {'coverage':'missing'}}})
    errors = [i for i in preflight_config(config, environ={'LOCAL_VLLM_API_KEY':'local'}) if i['code']=='prompt_missing']
    assert [i['path'] for i in errors] == ['point_extractor.coverage']


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
