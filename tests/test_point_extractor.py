"""Public extractor entry point after migration to the single Harness path."""
import json
from pathlib import Path
import pytest
from backend.env import ModelCallOptions
from novelty_agent_framework.agents import DemoPointExtractor, NoveltyPointExtractorAgent, build_paper_digest
from novelty_agent_framework.core.extractor_harness import ExtractorHarnessError
from novelty_agent_framework.persistence import persist_novelty_points
from novelty_agent_framework.schemas import PaperDigest, PaperInput
from test_extractor_controlled import Client, baseline, point, setup

def make_paper() -> PaperInput:
    return PaperInput(
        paper_id="paper-1",
        title="测试论文",
        abstract="测试论文摘要",
        full_text="测试论文正文",
        claimed_contributions=["贡献一", "贡献二"],
    )


def test_real_paper_digest_includes_bounded_independent_contribution():
    path = Path(__file__).parent / "fixtures" / "extractor" / "real_paper_input.json"
    digest = build_paper_digest(PaperInput.model_validate_json(path.read_text()))
    assert "Sketch-DBH" in digest.full_text_excerpt
    assert "Count-Min Sketch" in digest.full_text_excerpt
    assert len(digest.full_text_excerpt) <= 2000 + 3 * 1200 + 150


def test_demo_extractor_from_contributions():
    digest = PaperDigest(
        paper_id="p",
        title="标题",
        abstract="摘要",
        claimed_contributions=["贡献一", "贡献二"],
    )
    points = DemoPointExtractor().extract(digest, previous_brief=None, attempt=1)

    assert [point.point_id for point in points] == ["NP-1", "NP-2"]
    assert points[0].source_locations == ["claimed_contributions"]


def test_demo_extractor_falls_back_to_abstract():
    digest = PaperDigest(
        paper_id="p",
        title="标题",
        abstract="这是第一句创新点。这是第二句创新点。",
        claimed_contributions=[],
    )
    points = DemoPointExtractor().extract(digest, previous_brief=None, attempt=1)

    assert len(points) >= 2
    assert points[0].source_locations == ["abstract"]


def test_build_paper_digest_truncates():
    paper = PaperInput(
        paper_id="p",
        title="t",
        abstract="a",
        full_text="x" * 3000,
        references=["r" * 200] * 40,
    )
    digest = build_paper_digest(paper)

    assert digest.references == []  # 当前不向提取模型提供参考文献
    assert digest.full_text_excerpt.endswith("…[截断]")
    assert len(digest.full_text_excerpt) <= 2000 + len("…[截断]")


def test_build_paper_digest_includes_english_fields():
    paper = PaperInput(
        paper_id="p",
        title="t",
        abstract="中文摘要",
        english_abstract="English abstract",
        full_text="x",
        keywords_zh=["图神经网络"],
        keywords_en=["graph neural network"],
    )

    digest = build_paper_digest(paper)

    assert digest.english_abstract == "English abstract"
    assert digest.keywords_zh == ["图神经网络"]
    assert digest.keywords_en == ["graph neural network"]


def test_build_paper_digest_excerpt_skips_cover():
    paper = PaperInput(
        paper_id="p",
        title="t",
        abstract="摘要正文。",
        full_text=(
            "# Page 1\n研究生毕业论文封面信息\n\n# Page 3\n摘 要\n"
            "摘要正文。\n后续正文内容。"
        ),
    )

    digest = build_paper_digest(paper)

    assert digest.full_text_excerpt.startswith("摘要正文。")
    assert "封面信息" not in digest.full_text_excerpt


def test_demo_extractor_caps_at_three():
    digest = PaperDigest(
        paper_id="p",
        title="标题",
        abstract=(
            "第一句创新点内容。第二句创新点内容。第三句创新点内容。"
            "第四句创新点内容。第五句创新点内容。"
        ),
        claimed_contributions=[],
    )

    points = DemoPointExtractor().extract(digest, previous_brief=None, attempt=1)

    assert len(points) == 3
    assert [point.point_id for point in points] == ["NP-1", "NP-2", "NP-3"]


def test_persist_novelty_points_writes_file(tmp_path):
    paper = make_paper()
    points = DemoPointExtractor().extract(
        build_paper_digest(paper),
        previous_brief=None,
        attempt=1,
    )

    path = persist_novelty_points(paper, list(points), output_root=tmp_path)
    data = json.loads(path.read_text(encoding="utf-8"))

    assert path == tmp_path / "paper-1" / "novelty-points.json"
    assert data["paper_id"] == "paper-1"
    assert data["storage"] == "test-version-local-file"
    assert [point["point_id"] for point in data["novelty_points"]] == ["NP-1", "NP-2"]


def test_direct_constructor_uses_harness_without_opt_in(tmp_path):
    _, client, digest, _ = setup(tmp_path, baseline)
    options = ModelCallOptions(temperature=0, max_tokens=4096)
    agent = NoveltyPointExtractorAgent(client, model_options=options)
    result = agent.extract(digest, previous_brief=None, attempt=1)
    assert len(result) == 1
    assert agent.last_trace['harness'] == 'extractor_choices_v6'
    assert client.calls[0]['digest'] == digest.model_dump(mode='json')
    assert client.calls[0]['content_list']
    assert agent.model_options is options


def test_digest_without_persisted_source_fails_before_model_call():
    client = Client(lambda ctx: pytest.fail('No model call before input validation'))
    agent = NoveltyPointExtractorAgent(client)
    with pytest.raises(ExtractorHarnessError, match='requires persisted PaperInput'):
        agent.extract(PaperDigest(paper_id='p'), previous_brief=None, attempt=1)
    assert not client.calls
    assert agent.last_trace['execution_status'] == 'failed'


@pytest.mark.parametrize('options', [dict(harness_config={'enabled':False}),
    dict(reading_prompt_name='extractor/read_paper'), dict(conservative_dedup=False)])
def test_removed_legacy_entry_cannot_silently_fall_back(options):
    with pytest.raises((TypeError, ValueError)):
        NoveltyPointExtractorAgent(**options)


@pytest.mark.parametrize('english', [True, False])
def test_bilingual_fields_survive_harness(tmp_path, english):
    def handler(ctx):
        if ctx['phase'] == 'generate':
            p = point(ctx)
            if english:
                p.update(claim_en='Event-triggered scheduling', technical_features_en=['Dynamic queue'])
            return {'option_id':ctx['option_id'], 'points':[p]}
        return baseline(ctx)
    agent, _, digest, _ = setup(tmp_path, handler)
    result = agent.extract(digest, previous_brief=None, attempt=1)
    assert result[0].claim_en == ('Event-triggered scheduling' if english else '')
    assert result[0].technical_features_en == (['Dynamic queue'] if english else [])


def test_oversized_generation_is_corrected_without_silently_truncating(tmp_path):
    first = True
    def handler(ctx):
        nonlocal first
        if ctx['phase'] == 'generate':
            count = 10 if first else 8
            first = False
            return {'option_id':ctx['option_id'], 'points':[point(ctx, f'独立机制{i}') for i in range(count)]}
        return baseline(ctx)
    agent, _, digest, _ = setup(tmp_path, handler)
    result = agent.extract(digest, previous_brief=None, attempt=1)
    assert len(result) == 8
    assert agent.last_trace['calls'][1]['validation_status'] == 'failed'
    assert agent.last_trace['corrections'][0]['status'] == 'verified'
