"""按 Manifest Artifact ID 安全读取参考文献文本片段。"""

from __future__ import annotations

import asyncio

from ..persistence import ReferenceStore, reference_store_for_artifact_namespace
from ..schemas import ArtifactNamespace, ReferenceReadRequest, ReferenceReadResult


class ReferenceArtifactReaderTool:
    name = "reference_artifact_reader"
    description = "读取已保存参考文献 Artifact 的受限文本片段。"

    def __init__(
        self,
        reference_store: ReferenceStore | None = None,
        *,
        max_chars_per_read: int = 16_000,
    ) -> None:
        if not 1 <= max_chars_per_read <= 16_000:
            raise ValueError("max_chars_per_read must be in 1..16000")
        self.reference_store = reference_store or ReferenceStore()
        self.max_chars_per_read = max_chars_per_read

    def locate(self, paper_id: str, artifact_id: str) -> ArtifactNamespace | None:
        """返回该 artifact_id 实际所在的公开命名空间，研究语料优先；都没有则 None。

        只查两份 Manifest，不触碰磁盘。存在的 id 保证可解析，冲突（同一个裸 id
        同时存在于两个语料）在真实数据里不会出现——两边 id 的派生输入完全不同。
        """

        for namespace in (
            ArtifactNamespace.RESEARCH_REFERENCE,
            ArtifactNamespace.SUBJECT_REFERENCE,
        ):
            store = reference_store_for_artifact_namespace(
                namespace, output_root=self.reference_store.output_root
            )
            if store.find_artifact(paper_id, artifact_id) is not None:
                return namespace
        return None

    async def ainvoke(self, request: ReferenceReadRequest) -> ReferenceReadResult:
        request = ReferenceReadRequest.model_validate(request)
        if request.max_chars > self.max_chars_per_read:
            raise ValueError(
                f"max_chars exceeds reader limit {self.max_chars_per_read}"
            )
        store = reference_store_for_artifact_namespace(
            request.namespace, output_root=self.reference_store.output_root
        )
        return store.read_document_slice(
            request.subject_paper_id,
            artifact_id=request.artifact_id,
            char_start=request.char_start,
            max_chars=request.max_chars,
        )

    def invoke(self, request: ReferenceReadRequest) -> ReferenceReadResult:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(self.ainvoke(request))
        raise RuntimeError(
            "检测到正在运行的事件循环，请改用 await reader.ainvoke(...)"
        )
