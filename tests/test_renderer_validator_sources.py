"""Validator source provenance is versioned and shown in both report templates."""
import json

import pytest

from novelty_agent_framework.persistence import persist_evidence_cards
from novelty_agent_framework.report_inputs import record_report_input_files
from novelty_agent_framework.schemas import EvidenceCard, PaperInput, RejectedEvidence
from novelty_agent_framework.tools.renderer import ReportRenderError, render_report
from test_renderer import refresh_fixture_versions, seed_workspace


def test_actual_validator_source_snapshot_is_preserved_and_rendered(tmp_path):
    seed_workspace(tmp_path)
    workspace = tmp_path / 'renderer-paper'
    evidence = json.loads((workspace / 'evidence-cards.json').read_text())
    paper = PaperInput.model_validate_json((workspace / 'paper-input/others/paper.json').read_text())
    def cards(key):
        return [EvidenceCard.model_validate(x) for x in evidence.get(key, [])]
    integrity = {'metadata_mode': 'external', 'metadata_query_count': 1,
        'metadata_authenticity': {'C1': {'sources': [{'retrieval': {'providers': [
            {'source_id': 'datacite', 'status': 'found'},
            {'source_id': 'crossref', 'status': 'error'},
            {'source_id': 'doi_org', 'status': 'unsupported'},
        ]}}]}}}
    persist_evidence_cards(paper, raw_cards=cards('raw_evidence_cards'),
        accepted_cards=cards('accepted_evidence_cards'),
        rejected_evidence=[RejectedEvidence.model_validate(x) for x in evidence.get('rejected_evidence', [])],
        validator_integrity=integrity, output_root=tmp_path)
    if (workspace / 'novelty-reviews.json').exists():
        record_report_input_files(workspace, ('novelty-reviews.json',))
    refresh_fixture_versions(workspace)
    for name in ('default', 'concise'):
        path = render_report(paper_name='renderer-paper', output_root=tmp_path, template_name=name)
        text = path.read_text()
        assert 'DataCite' in text
        assert 'Crossref' in text
        assert 'arXiv' in text.split('检索来源', 1)[1].split('###', 1)[0]
        assert '已返回结果' not in text
        assert '部分查询未完成' not in text
        assert 'DOI.org' not in text
        heading = '## 备注' if name == 'default' else '## 八、备注'
        assert text.index('查新结论') < text.index(heading)
        assert '1. 本报告由大语言模型生成，请注意。' in text
    path = workspace / 'evidence-cards.json'
    data = json.loads(path.read_text())
    data['validator_sources']['providers'][0]['source_id'] = 'tampered'
    path.write_text(json.dumps(data))
    with pytest.raises(ReportRenderError, match='版本不一致'):
        render_report(paper_name='renderer-paper', output_root=tmp_path)


def test_historical_report_does_not_invent_validator_sources(tmp_path):
    seed_workspace(tmp_path)
    for name in ('default', 'concise'):
        text = render_report(paper_name='renderer-paper', output_root=tmp_path,
                             template_name=name).read_text()
        assert 'Crossref' not in text and 'DataCite' not in text


def test_only_used_providers_are_combined_across_validator_rounds():
    from novelty_agent_framework.persistence import validator_sources_index
    from novelty_agent_framework.tools.renderer import _validator_source_names

    def build(providers):
        return validator_sources_index({'metadata_mode': 'external', 'metadata_query_count': 1,
            'metadata_authenticity': {'card': {'sources': [{'retrieval': {'providers': providers}}]}}})

    first = build([{'source_id': 'crossref', 'status': 'found'},
                   {'source_id': 'doi_org', 'status': 'unsupported'}])
    second = build([{'source_id': 'datacite', 'status': 'not_found'},
                    {'source_id': 'crossref', 'status': 'error',
                     'error_type': 'ProviderPhysicalBudgetExceeded'},
                    {'source_id': 'other', 'status': 'error', 'error_type': 'ReadTimeout'}])
    assert _validator_source_names({'validator_sources_history': [first, second]}) == [
        'Crossref', 'DataCite', 'other']
    assert _validator_source_names({'validator_sources_history': [second]}) == ['DataCite', 'other']


def test_both_templates_list_executed_sources_without_results_or_cards(tmp_path):
    seed_workspace(tmp_path)
    workspace = tmp_path / 'renderer-paper'
    path = workspace / 'retrieval-plans.json'
    plans = json.loads(path.read_text())
    plans['novelty_point_plans'][0]['executed_queries'] = [
        {'database': 'arxiv', 'status': 'succeeded', 'results': []},
        {'database': 'springer', 'status': 'failed', 'results': []},
        {'database': 'unused', 'status': 'not_executed', 'results': []},
    ]
    path.write_text(json.dumps(plans))
    path = workspace / 'evidence-cards.json'
    evidence = json.loads(path.read_text())
    evidence['accepted_evidence_cards'] = []
    evidence['raw_evidence_cards'] = []
    evidence['validator_sources_history'] = [{'metadata_mode': 'external', 'providers': [
        {'source_id': 'doi_org', 'statuses': ['not_found']},
        {'source_id': 'crossref', 'statuses': ['found']},
        {'source_id': 'datacite', 'statuses': ['error']},
    ]}]
    path.write_text(json.dumps(evidence))
    record_report_input_files(workspace, ('retrieval-plans.json', 'evidence-cards.json'))
    refresh_fixture_versions(workspace)
    for name in ('default', 'concise'):
        text = render_report(paper_name='renderer-paper', output_root=tmp_path,
                             template_name=name).read_text()
        sources = text.split('检索来源', 1)[1].split('###', 1)[0]
        for source in ('arXiv', 'springer', 'DOI.org', 'Crossref', 'DataCite'):
            assert source in sources
        assert 'unused' not in sources
