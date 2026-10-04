"""混合检索保留稳定来源和投标方隔离测试。"""

import numpy as np
import pytest

from app.core.config import get_settings
from app.llm.rerank_model import RerankResult
from app.retrieval.models import IndexChunk, IndexFilter, IndexManifest
from app.retrieval.service import RetrievalService
from app.retrieval.store import LoadedIndex


class FakeEmbedding:
    async def embed_query(self, query):
        return (await self.embed([query]))[0]

    async def embed(self, texts):
        return np.asarray([[1.0, 0.0] for _ in texts], dtype=np.float32)


class FakeRerank:
    async def rerank(self, query, documents, top_n):
        return [
            RerankResult(document_id=item.document_id, score=0.8)
            for item in documents[:top_n]
        ]


@pytest.mark.asyncio
async def test_search_returns_only_current_bidder(monkeypatch):
    chunks = [
        IndexChunk(
            chunk_id="chunk-a",
            file_id="file-a",
            project_id=None,
            bidder_id="bidder-a",
            data_role="evidence_material",
            file_name="A合同.docx",
            source_path="A合同.docx",
            section="合同",
            content="A_ONLY_MARKER",
            parse_status="parsed",
            evidence_type="contract",
        ),
        IndexChunk(
            chunk_id="chunk-b",
            file_id="file-b",
            project_id=None,
            bidder_id="bidder-b",
            data_role="evidence_material",
            file_name="B合同.docx",
            source_path="B合同.docx",
            section="合同",
            content="B_ONLY_MARKER",
            parse_status="parsed",
            evidence_type="contract",
        ),
    ]
    loaded = LoadedIndex(
        IndexManifest(
            dimension=2,
            chunk_count=2,
            embedding_model="test",
            embedding_fingerprint="test",
        ),
        chunks,
        np.asarray([[1.0, 0.0], [1.0, 0.0]], dtype=np.float32),
        {},
    )
    monkeypatch.setattr(
        "app.retrieval.service.load_index", lambda *args, **kwargs: loaded
    )
    service = RetrievalService(FakeEmbedding(), FakeRerank(), get_settings())

    results = await service.search(
        "B_ONLY_MARKER",
        IndexFilter(
            scope_id="evidence-project-1-bidder-a",
            project_id=None,
            bidder_id="bidder-a",
            data_role="evidence_material",
        ),
    )

    assert [item.chunk_id for item in results] == ["chunk-a"]
    assert results[0].bidder_id == "bidder-a"


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["text", "table", "image"])
@pytest.mark.parametrize("candidate_k, final_k", [(30, 10), (3, 2)])
async def test_full_chunks_and_quotes_keep_top_k_contract(
    monkeypatch, temp_settings, kind, candidate_k, final_k
):
    settings = temp_settings.model_copy(
        update={
            "retrieval": temp_settings.retrieval.model_copy(
                update={
                    "candidate_top_k": candidate_k,
                    "final_top_k": final_k,
                }
            ),
        }
    )
    chunks = [
        IndexChunk(
            chunk_id=f"chunk-{i}",
            file_id="file-a",
            project_id=None,
            bidder_id="bidder-a",
            data_role="evidence_material",
            file_name="合同.md",
            source_path="合同.md",
            section="正文",
            content=("|长片段|完整数据|\n" * 1000) + f"末尾-{i}",
            parse_status="parsed",
            block_kind=kind,
        )
        for i in range(40)
    ]
    loaded = LoadedIndex(
        IndexManifest(
            dimension=2,
            chunk_count=40,
            embedding_model="test",
            embedding_fingerprint="test",
        ),
        chunks,
        np.ones((40, 2), dtype=np.float32),
        {},
    )
    seen = []

    def keywords(query, chunks, index, allowed, top_k):
        assert top_k == candidate_k and len(allowed) == 40
        return [(i, 1.0) for i in range(top_k)]

    class RecordingRerank(FakeRerank):
        async def rerank(self, query, documents, top_n):
            seen.append((documents, top_n))
            assert candidate_k <= len(documents) <= candidate_k * 2
            assert all(len(item.text) > 4000 for item in documents)
            return await super().rerank(query, documents, top_n)

    monkeypatch.setattr("app.retrieval.service.keyword_top_k", keywords)
    service = RetrievalService(
        FakeEmbedding(), RecordingRerank(), settings, indexes={"scope": loaded}
    )
    results = await service.search(
        "合同",
        IndexFilter(
            scope_id="scope", bidder_id="bidder-a", data_role="evidence_material"
        ),
    )
    assert len(results) == final_k and seen[0][1] == final_k
    by_id = {item.chunk_id: item for item in chunks}
    for result in results:
        assert result.content == result.source.quote == by_id[result.chunk_id].content
        assert not result.truncated
