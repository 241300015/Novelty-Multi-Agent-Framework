"""Deterministic EvidenceCardBuilder provenance and identity tests."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from novelty_agent_framework.agents import DefaultEvidenceValidator
from novelty_agent_framework.persistence import ReferenceStore
from novelty_agent_framework.schemas import (
    AccessStatus,
    Artifact,
    ArtifactNamespace,
    ArtifactRole,
    ContentExtent,
    EvidenceCardDraft,
    EvidenceLocator,
    EvidenceQuoteDraft,
    ExternalIdentifier,
    NoveltyPoint,
    ReferenceReadResult,
    ResearchFinishDraft,
    ResearchTask,
    SourceKind,
    SourceRecord,
    TaskResearchRequest,
    Work,
    WorkType,
)
from novelty_agent_framework.tools import EvidenceCardBuilder, ResearcherToolRegistry
from novelty_agent_framework.tools.evidence_card_builder import _quote_matches
from conftest import minimal_search_plan

NOW = datetime(2026, 8, 25, tzinfo=timezone.utc)
SHA = "a" * 64
TEXT_A = "Alpha unique quote. Shared technical statement. Another A quote."
TEXT_B = "Beta unique quote. Shared technical statement. Another B quote."


def scope() -> TaskResearchRequest:
    return TaskResearchRequest(
        subject_paper_id="paper-1",
        run_id="run-builder",
        novelty_point=NoveltyPoint(
            point_id="NP-1", claim="claim", technical_features=["feature"]
        ),
        research_task=ResearchTask(
            task_id="TASK-1",
            novelty_point_id="NP-1",
            task_type="evidence",
            language="en",
        ),
        search_plan=minimal_search_plan("TASK-1", "NP-1"),
    )


def prepare_store(tmp_path) -> ReferenceStore:
    store = ReferenceStore(tmp_path)
    manifest = store.load_manifest("paper-1")
    works = [
        Work(
            work_id="wrk_a",
            work_type=WorkType.WEBPAGE,
            title="Trusted Work A",
            identifiers=[ExternalIdentifier(namespace="doi", value="10.1/work-a")],
            canonical_source_record_id="src_a",
        ),
        Work(work_id="wrk_b", work_type=WorkType.WEBPAGE, title="Trusted Work B"),
    ]
    records = [
        SourceRecord(
            source_record_id="src_a",
            work_id="wrk_a",
            source_id="test",
            source_kind=SourceKind.STRUCTURED_DATABASE,
            title="Source A",
            landing_url="https://landing.example/a",
            full_text_url="https://full.example/a",
            access_status=AccessStatus.FULL_TEXT_ACQUIRED,
            observed_at=NOW,
        ),
        SourceRecord(
            source_record_id="src_b",
            work_id="wrk_b",
            source_id="test",
            source_kind=SourceKind.STRUCTURED_DATABASE,
            title="Source B",
            landing_url="https://landing.example/b",
            identifiers=[ExternalIdentifier(namespace="doi", value="10.1/source-b")],
            access_status=AccessStatus.FULL_TEXT_ACQUIRED,
            observed_at=NOW,
        ),
    ]
    artifacts = [
        Artifact(
            artifact_id="art_a",
            work_id="wrk_a",
            source_record_id="src_a",
            role=ArtifactRole.EXTRACTED_TEXT,
            media_type="text/plain",
            relative_path="documents/wrk_a/art_a.txt",
            sha256=SHA,
            content_extent=ContentExtent.FULL,
            acquired_at=NOW,
        ),
        Artifact(
            artifact_id="art_b",
            work_id="wrk_b",
            source_record_id="src_b",
            role=ArtifactRole.EXTRACTED_TEXT,
            media_type="text/plain",
            relative_path="documents/wrk_b/art_b.txt",
            sha256="b" * 64,
            content_extent=ContentExtent.FULL,
            acquired_at=NOW,
        ),
    ]
    store.persist_manifest(
        "paper-1",
        manifest.model_copy(
            update={"works": works, "source_records": records, "artifacts": artifacts}
        ),
    )
    return store


def read(work="wrk_a", artifact="art_a", text=TEXT_A, **updates):
    values = {
        "namespace": ArtifactNamespace.RESEARCH_REFERENCE,
        "read_id": f"read_{work}",
        "work_id": work,
        "artifact_id": artifact,
        "role": ArtifactRole.EXTRACTED_TEXT,
        "char_start": 0,
        "char_end": len(text),
        "text": text,
        "has_more": False,
        "sha256": SHA,
    }
    values.update(updates)
    return ReferenceReadResult(**values)


def quote(text, interpretation="meaning"):
    return EvidenceQuoteDraft(
        quote=text, interpretation=interpretation, confidence=0.9
    )


def card(*quotes):
    return EvidenceCardDraft(
        main_contribution="Model semantic contribution",
        overlaps=["overlap"],
        differences=["difference"],
        quotes=list(quotes),
        possible_baseline=True,
        relevance=0.8,
        confidence=0.9,
    )


def finish(*cards):
    return ResearchFinishDraft(cards=list(cards))


def builder(tmp_path):
    return EvidenceCardBuilder(prepare_store(tmp_path))


def test_single_quote_builds_trusted_card_and_evidence(tmp_path) -> None:
    result = builder(tmp_path).build(
        finish(card(quote("Alpha unique quote."))),
        scope=scope(),
        read_results=[read()],
    )

    assert len(result.evidence) == len(result.evidence_cards) == 1
    evidence = result.evidence[0]
    built = result.evidence_cards[0]
    assert (evidence.task_id, evidence.novelty_point_id) == ("TASK-1", "NP-1")
    assert (evidence.work_id, evidence.artifact_id) == ("wrk_a", "art_a")
    assert evidence.locator == EvidenceLocator(char_start=0, char_end=19)
    assert evidence.provenance == {
        "source_kind": "structured_database",
        "evidence_type": "database_evidence",
        "builder": "evidence_card_builder",
        "artifact_namespace": "research_reference",
        "read_id": "read_wrk_a",
        "read_char_start": 0,
        "read_char_end": len(TEXT_A),
        "quote_char_start": 0,
        "quote_char_end": 19,
        "source_record_id": "src_a",
    }
    assert built.document_title == "Trusted Work A"
    assert built.sources[0].url == "https://full.example/a"
    assert built.sources[0].doi == "10.1/work-a"
    assert built.sources[0].location == "artifact art_a chars:0-19"
    assert built.cited_by_paper is None
    assert built.evidence_ids == [evidence.evidence_id]


def test_locator_uses_absolute_read_offset(tmp_path) -> None:
    result = builder(tmp_path).build(
        finish(card(quote("Alpha unique quote."))),
        scope=scope(),
        read_results=[read(char_start=100, char_end=100 + len(TEXT_A))],
    )

    evidence = result.evidence[0]
    source = result.evidence_cards[0].sources[0]
    assert evidence.locator == EvidenceLocator(char_start=100, char_end=119)
    assert evidence.provenance["quote_char_start"] == 100
    assert evidence.provenance["quote_char_end"] == 119
    assert source.location == "artifact art_a chars:100-119"


def test_whitespace_normalized_quote_maps_to_original_offsets(tmp_path) -> None:
    prefix = "prefix\n"
    segment = "Alpha \t unique\nquote."
    text = prefix + segment + " suffix"
    result = builder(tmp_path).build(
        finish(card(quote("Alpha   unique\nquote."))),
        scope=scope(),
        read_results=[read(text=text, char_start=500, char_end=500 + len(text))],
    )

    evidence = result.evidence[0]
    expected_start = 500 + len(prefix)
    expected_end = 500 + len(prefix) + len(segment)
    assert evidence.locator == EvidenceLocator(
        char_start=expected_start, char_end=expected_end
    )
    assert text[evidence.locator.char_start - 500 : evidence.locator.char_end - 500] == segment
    assert result.evidence_cards[0].sources[0].location == (
        f"artifact art_a chars:{expected_start}-{expected_end}"
    )


def test_evidence_locators_stay_inside_read_bounds(tmp_path) -> None:
    result = builder(tmp_path).build(
        finish(card(quote("Alpha unique quote."), quote("Another A quote."))),
        scope=scope(),
        read_results=[read(char_start=10, char_end=10 + len(TEXT_A))],
    )
    for evidence in result.evidence:
        assert evidence.locator is not None
        assert evidence.locator.char_start >= 10
        assert evidence.locator.char_end <= 10 + len(TEXT_A)

def test_built_card_passes_default_evidence_validator(tmp_path) -> None:
    import hashlib

    evidence_builder = builder(tmp_path)
    store = evidence_builder.reference_store
    manifest = store.load_manifest("paper-1")
    for artifact, text in zip(manifest.artifacts, (TEXT_A, TEXT_B), strict=True):
        store.write_document("paper-1", work_id=artifact.work_id,
            artifact_id=artifact.artifact_id, extension="txt", content=text)
        artifact.sha256 = hashlib.sha256(text.encode()).hexdigest()
    store.persist_manifest("paper-1", manifest)
    result = evidence_builder.build(
        finish(card(quote("Alpha unique quote."))), scope=scope(),
        read_results=[read(sha256=manifest.artifacts[0].sha256)],
    )
    validation = DefaultEvidenceValidator().validate(
        result.evidence_cards, tasks=[scope().research_task], evidence=result.evidence,
        novelty_points=[scope().novelty_point], paper_id="paper-1", reference_store=store,
    )
    assert validation.accepted == tuple(result.evidence_cards), validation.rejected
    assert validation.rejected == ()


def test_multiple_reads_quotes_and_whitespace_grounding(tmp_path) -> None:
    duplicate = read(
        read_id="read_later",
        char_start=10,
        char_end=10 + len(TEXT_A),
    )
    result = builder(tmp_path).build(
        finish(
            card(
                quote("Alpha   unique\nquote."),
                quote("Another A quote."),
            )
        ),
        scope=scope(),
        read_results=[duplicate, read()],
    )

    assert len(result.evidence) == 2
    assert {item.work_id for item in result.evidence} == {"wrk_a"}
    assert all(item.provenance["read_id"] == "read_wrk_a" for item in result.evidence)


def test_ungrounded_and_ambiguous_quotes_drop_only_that_card(
    tmp_path,
) -> None:
    """回归守卫：单张卡的溯源问题只丢弃该卡，不作废同任务其它卡。

    此前 build() 直接向上抛，一条引文失配就作废整轮检索——实测某次 live 运行
    已经产出 finish draft，却因此整轮 0 张卡。
    """

    compiler = builder(tmp_path)
    reads = [read(), read("wrk_b", "art_b", TEXT_B)]

    for draft, reason in (
        (finish(card(quote("Paraphrased but absent"))), "ungrounded quote"),
        (finish(card(quote("Shared technical statement."))), "ambiguous"),
    ):
        result = compiler.build(draft, scope=scope(), read_results=reads)
        assert result.evidence_cards == []
        assert any(reason in warning for warning in result.warnings)

    # 同一份 draft 里混入可溯源的好卡时，好卡必须留下
    mixed = compiler.build(
        finish(
            card(quote("Paraphrased but absent")),
            card(quote("Alpha unique quote.")),
        ),
        scope=scope(),
        read_results=reads,
    )
    assert [item.document_title for item in mixed.evidence_cards] == ["Trusted Work A"]
    assert any("ungrounded quote" in warning for warning in mixed.warnings)


def test_candidate_intersection_disambiguates_shared_quote(tmp_path) -> None:
    result = builder(tmp_path).build(
        finish(card(quote("Shared technical statement."), quote("Alpha unique quote."))),
        scope=scope(),
        read_results=[read(), read("wrk_b", "art_b", TEXT_B)],
    )
    assert result.evidence_cards[0].document_title == "Trusted Work A"
    assert {item.work_id for item in result.evidence} == {"wrk_a"}


def test_missing_or_mismatched_artifact_drops_the_card(tmp_path) -> None:
    compiler = builder(tmp_path)
    result = compiler.build(
        finish(card(quote("Alpha unique quote."))),
        scope=scope(), read_results=[read(artifact="art_missing")],
    )
    assert result.evidence_cards == []
    assert any("missing Artifact" in warning for warning in result.warnings)

    result = compiler.build(
        finish(card(quote("Alpha unique quote."))),
        scope=scope(), read_results=[read(work="wrk_b", artifact="art_a")],
    )
    assert result.evidence_cards == []
    assert any("Artifact.work_id mismatch" in warning for warning in result.warnings)


def test_duplicate_work_drafts_keep_the_first_card(tmp_path) -> None:
    duplicate = card(quote("Alpha unique quote."))
    result = builder(tmp_path).build(
        finish(duplicate, duplicate), scope=scope(), read_results=[read()]
    )
    assert len(result.evidence_cards) == 1
    assert any(
        "duplicate evidence card for work" in warning for warning in result.warnings
    )


def test_ids_and_duplicate_read_selection_are_deterministic(tmp_path) -> None:
    compiler = builder(tmp_path)
    draft = finish(card(quote("Alpha unique quote.")))
    reads = [
        read(read_id="read_z", char_start=0, char_end=len(TEXT_A)),
        read(read_id="read_a", char_start=0, char_end=len(TEXT_A)),
    ]
    first = compiler.build(draft, scope=scope(), read_results=reads)
    second = compiler.build(draft, scope=scope(), read_results=reversed(reads))
    assert first.evidence[0].evidence_id == second.evidence[0].evidence_id
    assert first.evidence_cards[0].card_id == second.evidence_cards[0].card_id
    assert first.evidence[0].provenance["read_id"] == "read_a"


def test_source_doi_fallback_and_model_cannot_override_metadata(tmp_path) -> None:
    result = builder(tmp_path).build(
        finish(card(quote("Beta unique quote."))),
        scope=scope(), read_results=[read("wrk_b", "art_b", TEXT_B)],
    )
    source = result.evidence_cards[0].sources[0]
    assert result.evidence_cards[0].document_title == "Trusted Work B"
    assert source.url == "https://landing.example/b"
    assert source.doi == "10.1/source-b"
    assert not {
        "work_id", "artifact_id", "source_record_id", "url", "doi", "document_title"
    }.intersection(EvidenceCardDraft.model_fields)


def test_no_evidence_finish_and_registry_boundary(tmp_path) -> None:
    compiler = builder(tmp_path)
    result = compiler.build(
        ResearchFinishDraft(cards=[], no_evidence_reason="No grounded source"),
        scope=scope(), read_results=[],
    )
    assert result.evidence == result.evidence_cards == []
    assert result.warnings == ["no evidence: No grounded source"]
    assert "evidence_card_builder" not in ResearcherToolRegistry().names


def test_quote_matching_folds_typographic_variants() -> None:
    """回归守卫：模型转述正文时会把排版字符写成 ASCII 等价形式。

    实测一条 224 字的引文仅因原文的弯引号 ’ 被写成直引号 ' 就被判为
    ungrounded，导致整轮 0 张证据卡。归一化必须容忍这类长度不变的字形差异，
    但绝不能放宽到「词面不同也能通过」。
    """

    source = "the Transformer\u2019s multi-head self-attention (MHSA) module \u2014 see Fig. 2"

    assert _quote_matches(
        "the Transformer's multi-head self-attention (MHSA) module - see Fig. 2",
        source,
    )
    # 词面不同仍然必须判否
    assert not _quote_matches(
        "the Transformer's multi-head self-attention (MHSA) mechanism - see Fig. 2",
        source,
    )


def test_grounded_mixed_card_splits_by_work_without_copying_joint_claims(tmp_path):
    result = builder(tmp_path).build(
        finish(card(quote("Alpha unique quote.", "A contribution"),
                    quote("Beta unique quote.", "B contribution"))),
        scope=scope(), read_results=[read(), read("wrk_b", "art_b", TEXT_B)],
    )
    assert not result.rejections
    assert [c.document_title for c in result.evidence_cards] == ["Trusted Work A", "Trusted Work B"]
    assert [c.main_contribution for c in result.evidence_cards] == ["A contribution", "B contribution"]
    for c in result.evidence_cards:
        assert not c.overlaps and not c.differences and not c.possible_baseline
        evidence = [e for e in result.evidence if e.evidence_id in c.evidence_ids]
        assert len({e.work_id for e in evidence}) == 1
        assert evidence[0].provenance["builder_split"]["original_card_index"] == 0
    assert len({c.card_id for c in result.evidence_cards}) == 2


def test_mixed_card_with_unassignable_shared_quote_is_not_blindly_split(tmp_path):
    result = builder(tmp_path).build(
        finish(card(quote("Alpha unique quote."), quote("Beta unique quote."),
                    quote("Shared technical statement."))),
        scope=scope(), read_results=[read(), read("wrk_b", "art_b", TEXT_B)],
    )
    assert not result.evidence_cards
    assert result.rejections
