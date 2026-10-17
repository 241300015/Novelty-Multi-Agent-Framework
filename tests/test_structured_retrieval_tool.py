from __future__ import annotations

import asyncio
import hashlib
import inspect
from dataclasses import replace

import pytest
from pydantic import ValidationError

from novelty_agent_framework.agents import DemoQueryAdapter
from novelty_agent_framework.persistence import ReferenceStore
from novelty_agent_framework.ports import FullText, SearchHit
from novelty_agent_framework.schemas import (
    ArtifactRole,
    EvidenceSource,
    NoveltyPoint,
    ResearchBundle,
    ResearchTask,
    SearchConcept,
    SearchPlan,
    SearchStrategy,
    StructuredSourceRetrievalRequest,
)
from novelty_agent_framework.tools.database_search import (
    RetrievalSource,
    StructuredSourceRetrievalTool,
)

ABSTRACT = "A complete abstract returned by the source."
FULL_TEXT = "Extracted full text returned by the source."


def point() -> NoveltyPoint:
    return NoveltyPoint(
        point_id="NP-1", claim="一种通信方法", technical_features=["通信"]
    )


def task() -> ResearchTask:
    return ResearchTask(
        task_id="T-1",
        novelty_point_id="NP-1",
        task_type="search",
        language="en",
    )


def request(**updates) -> StructuredSourceRetrievalRequest:
    data = {
        "subject_paper_id": "paper-1",
        "source_id": "demo",
        "novelty_point": point(),
        "research_task": task(),
        "search_plan": Planner().plan(point(), task()),
        "run_id": "run-1",
    }
    data.update(updates)
    return StructuredSourceRetrievalRequest(**data)


class Planner:
    def __init__(self, *, asynchronous=False):
        self.asynchronous = asynchronous
        self.calls = []

    def plan(self, novelty_point, research_task):
        self.calls.append((novelty_point.point_id, research_task.task_id))
        value = SearchPlan(
            task_id=research_task.task_id,
            novelty_point_id=novelty_point.point_id,
            concepts=[SearchConcept(concept_id="C1", name="core", terms=["term"])],
            strategies=[
                SearchStrategy(strategy_id="S1", level="strict", expression="C1"),
                SearchStrategy(strategy_id="S2", level="broad", expression="C1"),
            ],
        )
        if not self.asynchronous:
            return value

        async def result():
            return value

        return result()


def hit(doc_id="2305.12345", *, abstract=ABSTRACT, external_id="2305.12345v2"):
    return SearchHit(
        document_id=doc_id,
        source_id="demo",
        external_id=external_id,
        title=f"Paper {doc_id}",
        abstract=abstract,
        authors=("Alice",),
        year=2023,
        doi="10.1/example",
        url=f"https://example.test/{doc_id}",
        full_text_url=f"https://example.test/{doc_id}.pdf",
    )


class Searcher:
    source_id = "demo"

    def __init__(self, *, asynchronous=False, fail_first=False, values=None):
        self.asynchronous = asynchronous
        self.fail_first = fail_first
        self.values = [hit()] if values is None else values
        self.calls = []

    def search(self, query, *, limit=10):
        self.calls.append((query, limit))
        if self.fail_first and len(self.calls) == 1:
            raise RuntimeError("Authorization: secret-token")
        value = list(self.values)
        if not self.asynchronous:
            return value

        async def result():
            return value

        return result()


class Metadata:
    source_id = "demo"

    def __init__(self, *, fail=False):
        self.fail = fail
        self.calls = []

    def resolve(self, document_id):
        self.calls.append(document_id)
        if self.fail:
            raise RuntimeError("metadata unavailable")
        return EvidenceSource(
            title=f"Paper {document_id}",
            doi="10.1/example",
            url=f"https://example.test/{document_id}",
        )


class FullTexts:
    source_id = "demo"

    def __init__(self, *, fail_ids=()):
        self.fail_ids = set(fail_ids)
        self.calls = []

    def fetch(self, document_id):
        self.calls.append(document_id)
        if document_id in self.fail_ids:
            raise RuntimeError("full text unavailable")
        return FullText(
            document_id=document_id,
            title=f"Paper {document_id}",
            text=FULL_TEXT,
            content_extent="partial",
            source_url=f"https://example.test/full/{document_id}",
        )


def build_tool(
    tmp_path,
    *,
    asynchronous=False,
    fail_first=False,
    values=None,
    metadata=True,
    full_text=True,
    store=None,
):
    planner = Planner(asynchronous=asynchronous)
    searcher = Searcher(
        asynchronous=asynchronous, fail_first=fail_first, values=values
    )
    metadata_tool = Metadata() if metadata else None
    full_text_tool = FullTexts() if full_text else None
    source = RetrievalSource(
        source_id="demo",
        query_adapter=DemoQueryAdapter(),
        search_tool=searcher,
        metadata_tool=metadata_tool,
        full_text_tool=full_text_tool,
    )
    tool = StructuredSourceRetrievalTool(
        search_planner=planner,
        source=source,
        reference_store=store or ReferenceStore(tmp_path),
        candidate_limit=2,
    )
    return tool, planner, searcher, metadata_tool, full_text_tool


def test_candidate_limit_is_renewed_for_each_discovery_round(tmp_path):
    searcher = Searcher()
    tool = StructuredSourceRetrievalTool(
        source=RetrievalSource(source_id="demo", query_adapter=DemoQueryAdapter(), search_tool=searcher),
        reference_store=ReferenceStore(tmp_path), candidate_limit=8, full_text_limit=0)
    base = request()
    plan = base.search_plan.model_copy(update={"strategies": [
        SearchStrategy(strategy_id=f"S{i}", level=level, expression="C1")
        for i, level in enumerate(("strict", "medium", "broad"))]})

    async def run():
        work_ids = set()
        for index in range(3):
            searcher.values = [replace(hit(f"round{index}-{p}", external_id=f"round{index}-{p}"), doi=None)
                               for p in range(10)]
            bundle = await tool.ainvoke(base.model_copy(update={
                "search_plan": plan.model_copy(update={"strategies": plan.strategies[index:]})}))
            assert len(bundle.source_records) == 8
            work_ids.update(record.work_id for record in bundle.source_records)
        assert len(work_ids) == 24

    asyncio.run(run())


@pytest.mark.parametrize("asynchronous", [False, True])
def test_single_task_tool_returns_bundle_for_sync_and_async_fakes(
    tmp_path, asynchronous
):
    tool, planner, searcher, metadata, full_text = build_tool(
        tmp_path, asynchronous=asynchronous
    )
    bundle = asyncio.run(tool.ainvoke(request()))

    assert isinstance(bundle, ResearchBundle)
    assert bundle.producer == "structured_source_retrieval:demo"
    assert bundle.evidence == []
    assert planner.calls == []
    assert searcher.calls
    assert metadata.calls == ["2305.12345"]
    assert full_text.calls == ["2305.12345"]
    assert bundle.source_records[0].external_id == "2305.12345v2"
    assert {item.role for item in bundle.artifacts} == {
        ArtifactRole.ABSTRACT,
        ArtifactRole.EXTRACTED_TEXT,
    }


def test_request_and_source_are_validated_before_execution(tmp_path):
    with pytest.raises(ValidationError, match="must match"):
        request(
            research_task=task().model_copy(
                update={"novelty_point_id": "NP-other"}
            )
        )
    with pytest.raises(ValidationError, match="search_plan.task_id"):
        request(
            search_plan=request().search_plan.model_copy(update={"task_id": "T-other"})
        )
    with pytest.raises(ValidationError, match="search_plan.novelty_point_id"):
        request(
            search_plan=request().search_plan.model_copy(
                update={"novelty_point_id": "NP-other"}
            )
        )
    tool, _, searcher, _, _ = build_tool(tmp_path)
    with pytest.raises(ValueError, match="does not match"):
        asyncio.run(tool.ainvoke(request(source_id="other")))
    assert searcher.calls == []


def test_tool_constructor_has_no_researcher_or_validator_dependencies():
    parameters = inspect.signature(StructuredSourceRetrievalTool).parameters
    forbidden = {
        "research_agent",
        "literature_research_agent",
        "validator",
        "coordinator",
    }
    assert forbidden.isdisjoint(parameters)


def test_search_execution_failure_stops_chain(tmp_path):
    ordered = [hit("a", external_id="a-v2"), hit("b", external_id="b-v1")]
    tool, _, _, _, _ = build_tool(
        tmp_path, fail_first=True, values=ordered, full_text=False
    )
    bundle = asyncio.run(tool.ainvoke(request()))

    assert bundle.search_executions[0].status.value == "failed"
    assert all(item.status.value == "not_run" and
               item.parameters["not_run_reason"] == "provider_failed"
               for item in bundle.search_executions[1:])
    assert "secret-token" not in bundle.search_executions[0].error
    blocker = bundle.search_executions[0]
    for skipped in bundle.search_executions[1:]:
        assert skipped.parameters["blocked_by_execution_id"] == blocker.execution_id
        assert skipped.parameters["blocked_by_failure_event_id"] == blocker.failure.event_id
        assert skipped.parameters["blocked_by_failure_code"] == blocker.failure.code.value



def test_search_execution_rank_and_empty_success(tmp_path):
    ordered = [hit("a", external_id="a-v2"), hit("b", external_id="b-v1")]
    tool, _, _, _, _ = build_tool(
        tmp_path, values=ordered, full_text=False
    )
    bundle = asyncio.run(tool.ainvoke(request()))

    assert bundle.search_executions[0].status.value == "succeeded"
    assert [item.rank for item in bundle.search_executions[0].results] == [1, 2]

    empty_tool, _, _, _, _ = build_tool(tmp_path / "empty", values=[])
    empty = asyncio.run(
        empty_tool.ainvoke(request(subject_paper_id="empty-paper"))
    )
    assert empty.search_executions[0].status.value == "succeeded"
    assert empty.search_executions[0].results == []


def test_artifact_hash_path_content_and_extent(tmp_path):
    tool, _, _, _, _ = build_tool(tmp_path)
    bundle = asyncio.run(tool.ainvoke(request()))
    extracted = next(
        item for item in bundle.artifacts if item.role == ArtifactRole.EXTRACTED_TEXT
    )
    path = tmp_path / "paper-1/references" / extracted.relative_path
    assert path.read_text(encoding="utf-8") == FULL_TEXT
    assert extracted.sha256 == hashlib.sha256(FULL_TEXT.encode()).hexdigest()
    assert extracted.media_type == "text/plain"
    assert extracted.content_extent.value == "partial"
    assert extracted.relative_path.endswith(".txt")
    assert extracted.derived_from_artifact_id is None


def test_missing_optional_tools_still_returns_metadata_bundle(tmp_path):
    tool, _, _, _, _ = build_tool(
        tmp_path, metadata=False, full_text=False
    )
    bundle = asyncio.run(tool.ainvoke(request()))
    assert bundle.source_records
    assert {item.role for item in bundle.artifacts} == {ArtifactRole.ABSTRACT}
    assert bundle.evidence == []


def test_full_text_failure_is_warning_and_other_candidate_continues(tmp_path):
    values = [hit("a", external_id="a-v1"), hit("b", external_id="b-v1")]
    planner = Planner()
    searcher = Searcher(values=values)
    full_text = FullTexts(fail_ids={"a"})
    source = RetrievalSource(
        source_id="demo",
        query_adapter=DemoQueryAdapter(),
        search_tool=searcher,
        full_text_tool=full_text,
    )
    tool = StructuredSourceRetrievalTool(
        search_planner=planner,
        source=source,
        reference_store=ReferenceStore(tmp_path),
        candidate_limit=2,
    )
    bundle = asyncio.run(tool.ainvoke(request()))
    assert any("full text a" in warning for warning in bundle.warnings)
    extracted = [
        item for item in bundle.artifacts if item.role == ArtifactRole.EXTRACTED_TEXT
    ]
    assert len(extracted) == 1


def test_repeated_invocation_is_manifest_idempotent(tmp_path):
    tool, _, _, _, _ = build_tool(tmp_path)
    first = asyncio.run(tool.ainvoke(request()))
    second = asyncio.run(tool.ainvoke(request()))
    manifest = ReferenceStore(tmp_path).load_manifest("paper-1")
    assert len(first.works) == len(second.works) == len(manifest.works) == 1
    assert len(manifest.source_records) == 1
    assert len(manifest.artifacts) == 2


def test_artifact_failure_is_recoverable_and_not_added_to_manifest(tmp_path):
    class FailingStore(ReferenceStore):
        def write_document(self, *args, **kwargs):
            raise OSError("simulated write failure")

    store = FailingStore(tmp_path)
    tool, _, _, _, _ = build_tool(tmp_path, store=store)
    bundle = asyncio.run(tool.ainvoke(request()))
    assert bundle.works and bundle.source_records
    assert bundle.artifacts == []
    assert any("could not be saved" in warning for warning in bundle.warnings)
    assert ReferenceStore(tmp_path).load_manifest("paper-1").artifacts == []


def test_unknown_arxiv_version_warns_without_inventing_v1(tmp_path):
    arxiv_hit = hit(external_id=None)
    arxiv_hit = SearchHit(
        **{
            **arxiv_hit.__dict__,
            "source_id": "arxiv",
        }
    )
    planner = Planner()
    searcher = Searcher(values=[arxiv_hit])
    searcher.source_id = "arxiv"
    adapter = DemoQueryAdapter()
    adapter.database = "arxiv"
    source = RetrievalSource(
        source_id="arxiv", query_adapter=adapter, search_tool=searcher
    )
    tool = StructuredSourceRetrievalTool(
        search_planner=planner,
        source=source,
        reference_store=ReferenceStore(tmp_path),
    )
    bundle = asyncio.run(tool.ainvoke(request(source_id="arxiv")))
    assert bundle.source_records[0].external_id == "2305.12345"
    assert not bundle.source_records[0].external_id.endswith("v1")
    assert any("v1 was not inferred" in warning for warning in bundle.warnings)


def test_sync_invoke_rejects_running_event_loop(tmp_path):
    tool, _, _, _, _ = build_tool(tmp_path)

    async def call():
        with pytest.raises(RuntimeError, match="await tool.ainvoke"):
            tool.invoke(request())

    asyncio.run(call())


def test_concurrent_retrievals_do_not_orphan_artifacts(tmp_path):
    """回归守卫：并行研究任务不得让 manifest 丢更新。

    ``ainvoke`` 在 load_manifest 与 persist_manifest 之间跨越多次 await，
    若各自基于开头读到的副本整份写回，最后落盘者会抹掉其它任务写入的
    Work/SourceRecord/Artifact —— 正文文件仍在磁盘上，但清单里没有记录，
    reader 随后报 ``unknown artifact_id``，而且因为 reader 强制约束挡住了
    重新检索，这些制品再也补不回来。
    """

    class PerCallSearcher:
        """每次检索返回一篇不同的文献，让 4 个任务产出 4 组独立制品。"""

        source_id = "demo"

        def __init__(self) -> None:
            self.calls = 0

        def search(self, query, *, limit=10):
            self.calls += 1
            index = self.calls

            async def result():
                return [
                    SearchHit(
                        document_id=f"2305.{index:05d}",
                        source_id="demo",
                        external_id=f"2305.{index:05d}v1",
                        title=f"Paper {index}",
                        abstract=f"{ABSTRACT} call {index}",
                        authors=("Alice",),
                        year=2023,
                        doi=f"10.1/example-{index}",
                        url=f"https://example.test/{index}",
                        full_text_url=f"https://example.test/{index}.pdf",
                    )
                ]

            return result()

    source = RetrievalSource(
        source_id="demo",
        query_adapter=DemoQueryAdapter(),
        search_tool=PerCallSearcher(),
        metadata_tool=Metadata(),
        full_text_tool=FullTexts(),
    )
    store = ReferenceStore(tmp_path)
    tool = StructuredSourceRetrievalTool(
        search_planner=Planner(asynchronous=True),
        source=source,
        reference_store=store,
        candidate_limit=2,
    )

    async def run_all():
        return await asyncio.gather(*(tool.ainvoke(request()) for _ in range(4)))

    bundles = asyncio.run(run_all())

    produced = {item.artifact_id for bundle in bundles for item in bundle.artifacts}
    assert len(produced) >= 4, "每个任务都应当产出独立制品"

    manifest = store.load_manifest("paper-1")
    assert {item.artifact_id for item in manifest.artifacts} == produced, (
        "manifest 丢失了并发任务写入的制品"
    )
    on_disk = {
        path.stem
        for path in (tmp_path / "paper-1" / "references" / "documents").glob("*/*.txt")
    }
    assert on_disk == produced, "存在已落盘但未登记的悬空正文"
    assert {item.work_id for item in manifest.works} >= {
        item.work_id for bundle in bundles for item in bundle.works
    }
