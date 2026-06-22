"""Rerank 稳定 ID、排序和降级契约测试。"""

import pytest

from app.core.config import RerankSettings
from app.llm.rerank_model import RerankDocument, RerankModel


class FakeBackend:
    async def score(self, query, documents):
        return [0.2, 0.9]


class FailedBackend:
    async def score(self, query, documents):
        raise TimeoutError("timeout")


def settings(allow_degradation=True):
    return RerankSettings(
        base_url="http://localhost/rerank",
        api_key="secret",
        model="reranker",
        top_n=2,
        timeout_seconds=10,
        max_retries=1,
        allow_degradation=allow_degradation,
    )


@pytest.mark.asyncio
async def test_rerank_uses_document_id_for_result_mapping():
    model = RerankModel(settings(), FakeBackend())
    documents = [
        RerankDocument(document_id="chunk-a", text="same"),
        RerankDocument(document_id="chunk-b", text="same"),
    ]

    results = await model.rerank("query", documents)

    assert [item.document_id for item in results] == ["chunk-b", "chunk-a"]


@pytest.mark.asyncio
async def test_rerank_degradation_preserves_initial_order():
    model = RerankModel(settings(), FailedBackend())
    documents = [
        RerankDocument(document_id="chunk-a", text="a"),
        RerankDocument(document_id="chunk-b", text="b"),
    ]

    results = await model.rerank("query", documents)

    assert [item.document_id for item in results] == ["chunk-a", "chunk-b"]
    assert all(item.score is None for item in results)
