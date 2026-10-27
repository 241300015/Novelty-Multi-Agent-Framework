"""Source lineage and runtime skill integration contracts."""
import asyncio

import pytest

from novelty_agent_framework.schemas import ResearchFinishDraft, SourceKind
from novelty_agent_framework.tools import EvidenceCardBuilder
from test_evidence_card_builder import prepare_store, scope, read, card, quote
from test_card_recovery import Reader, workflow, call, finish
from test_tool_call_harness import ScriptedModelClient


@pytest.mark.parametrize('kind,expected', [
    (SourceKind.STRUCTURED_DATABASE, 'database_evidence'),
    (SourceKind.WEB, 'web_supplement_evidence'),
    (SourceKind.WEB_SUPPLEMENT, 'web_supplement_evidence'),
    (SourceKind.LOCAL, 'unknown'),
])
def test_builder_binds_evidence_type_from_persisted_source(tmp_path, kind, expected):
    store = prepare_store(tmp_path)
    manifest = store.load_manifest('paper-1')
    manifest.source_records[0].source_kind = kind
    store.persist_manifest('paper-1', manifest)
    built = EvidenceCardBuilder(store).build(
        ResearchFinishDraft(cards=[card(quote('Alpha unique quote.'))]),
        scope=scope(), read_results=[read()],
    )
    if kind in {SourceKind.WEB, SourceKind.WEB_SUPPLEMENT}:
        assert not built.evidence and not built.evidence_cards
        return
    assert len(built.evidence) == 1
    provenance = built.evidence[0].provenance
    assert provenance['evidence_type'] == expected
    if expected == 'web_supplement_evidence':
        assert provenance['source_kind'] == 'web_supplement'


def test_web_acquisition_does_not_become_database_evidence(tmp_path):
    store = prepare_store(tmp_path)
    manifest = store.load_manifest('paper-1')
    manifest.source_records[0].source_kind = SourceKind.STRUCTURED_DATABASE
    manifest.artifacts[0].provenance['source_kind'] = 'web_supplement'
    store.persist_manifest('paper-1', manifest)
    built = EvidenceCardBuilder(store).build(
        ResearchFinishDraft(cards=[card(quote('Alpha unique quote.'))]),
        scope=scope(), read_results=[read()],
    )
    assert not built.evidence and not built.evidence_cards


def test_reader_of_llm_summary_cannot_ground_original_evidence(tmp_path):
    store = prepare_store(tmp_path)
    manifest = store.load_manifest('paper-1')
    manifest.artifacts[0].provenance['content_origin'] = 'llm_summary'
    store.persist_manifest('paper-1', manifest)
    built = EvidenceCardBuilder(store).build(
        ResearchFinishDraft(cards=[card(quote('Alpha unique quote.'))]),
        scope=scope(), read_results=[read()],
    )
    assert not built.evidence and not built.evidence_cards
    assert built.rejections


def test_researcher_actually_loads_both_skills(tmp_path):
    model = ScriptedModelClient(call('a'), finish(card(quote('Alpha unique quote.'))))
    result = asyncio.run(workflow(tmp_path, model, Reader([read()])).ainvoke(scope()))
    assert result.evidence_cards
    system = model.calls[0][0][0].content
    assert 'Database → source artifact → text → Reader → Evidence' in system
    assert 'no papers' in system and 'search advice' in system
    assert 'source_kind = web_supplement' in system


@pytest.mark.parametrize('with_library', [False, True])
def test_bound_evidence_reaches_reviewer_without_runtime_provenance(tmp_path, with_library):
    from pathlib import Path
    from backend.env import ModelResponse, PromptLibrary
    from novelty_agent_framework.agents import NoveltyEvidenceReviewer
    from novelty_agent_framework.schemas import NoveltyPointReviewRequest
    from test_novelty_point_reviewer import _review_json

    store = prepare_store(tmp_path)
    built = EvidenceCardBuilder(store).build(
        ResearchFinishDraft(cards=[card(quote('Alpha unique quote.'))]),
        scope=scope(), read_results=[read()],
    )
    request = NoveltyPointReviewRequest(
        subject_paper_id=scope().subject_paper_id,
        novelty_point=scope().novelty_point, tasks=[scope().research_task],
        cards=built.evidence_cards, evidence=built.evidence,
    )
    model = ScriptedModelClient(ModelResponse(content=_review_json('insufficient_evidence')))
    prompts = PromptLibrary(Path('backend/src/novelty_agent_framework/prompts')) if with_library else None
    reviewer = NoveltyEvidenceReviewer(model, prompts=prompts)
    asyncio.run(reviewer.review(request))
    system, user = model.calls[0][0][:2]
    assert 'Evidence' in system.content
    assert 'web_supplement_evidence' in system.content
    assert built.evidence[0].evidence_id in user.content
    assert 'Alpha unique quote.' in user.content
    assert '"provenance"' not in user.content
    assert '"read_id"' not in user.content
    assert built.evidence[0].provenance['evidence_type'] == 'database_evidence'
