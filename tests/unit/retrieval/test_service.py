"""混合检索保留稳定来源和投标方隔离测试。"""

import numpy as np
import pytest

from app.core.config import get_settings
from app.llm.rerank_model import RerankResult
from app.retrieval.models import IndexChunk, IndexFilter, IndexManifest
from app.retrieval.service import RetrievalService
from app.retrieval.store import LoadedIndex


class FakeEmbedding:
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
