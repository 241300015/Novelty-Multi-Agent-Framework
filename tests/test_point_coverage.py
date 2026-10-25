"""Coverage boundaries in the single Extractor Harness."""
import pytest
from novelty_agent_framework.core.extractor_harness import ExtractorHarnessError
from test_extractor_controlled import setup, baseline, point, read, passed


def test_digest_and_directory_do_not_count_as_body_reading(tmp_path):
    agent, client, digest, _ = setup(tmp_path, baseline, max_read_chars=4000)
    agent.extract(digest, previous_brief=None, attempt=1)
    initial = client.calls[0]
    assert initial['digest'] == digest.model_dump(mode='json')
    assert len(initial['content_list']) == 3
    assert agent.last_trace['reading']['unread_segments']
    assert agent.last_trace['coverage_complete'] is False
    assert agent.last_trace['semantic_completeness_verified'] is False


def test_full_directory_survives_reading_option_budget_filter(tmp_path):
    def handler(ctx):
        if ctx['phase'] == 'reading_scope':
            assert len(ctx['content_list']) == 3
            assert len(ctx['options']) == 1  # only the shorter last segment fits
        return baseline(ctx)
    agent, client, digest, _ = setup(tmp_path, handler, max_read_chars=3000)
    agent.extract(digest, previous_brief=None, attempt=1)
    assert client.calls[-1]['content_list'] == client.calls[0]['content_list']
    assert agent.last_trace['post_generation_reading']['reason'] == 'no_readable_segments_within_budget'


def test_reading_all_segments_does_not_certify_semantic_coverage(tmp_path):
    def handler(ctx):
        if ctx['phase'] == 'reading_scope':
            return read(ctx, all=True)
        return baseline(ctx)
    agent, _, digest, _ = setup(tmp_path, handler)
    assert agent.extract(digest, previous_brief=None, attempt=1)
    assert not agent.last_trace['reading']['unread_segments']
    assert agent.last_trace['semantic_review_passed']
    assert agent.last_trace['semantic_completeness_verified'] is False


def test_unapproved_featureless_candidate_is_not_delivered(tmp_path):
    def handler(ctx):
        if ctx['phase'] == 'generate':
            p = point(ctx, '只有实验效果')
            p['technical_features'] = []
            return {'option_id':ctx['option_id'], 'points':[p]}
        return baseline(ctx)
    agent, _, digest, _ = setup(tmp_path, handler)
    with pytest.raises(ExtractorHarnessError):
        agent.extract(digest, previous_brief=None, attempt=1)
    assert not agent.last_trace.get('final_points')
    assert agent.last_trace['execution_status'] == 'failed'
