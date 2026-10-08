"""混合检索保留稳定来源和投标方隔离测试。"""

import numpy as np
import pytest
import httpx
import json

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
async def test_bailian_windows_only_change_scoring_copy_not_index_or_source(temp_settings, monkeypatch):
    from app.llm import bailian_retrieval as cloud
    from app.llm.rerank_model import RerankModel
    from tests.contract.test_bailian_retrieval import rerank_settings

    cfg = rerank_settings(instruction="rank")
    settings = temp_settings.model_copy(update={"rerank_model": cfg})
    monkeypatch.setattr(cloud, "RERANK_DOCUMENT_TARGET", 1800)
    content = '<table><tr><th>字段</th><th>数值</th></tr>' + ''.join(
        f'<tr><td rowspan="1">field-{i}</td><td colspan="1">{i} kg</td></tr>' for i in range(120)
    ) + '</table>TAIL_MARKER'
    chunk = IndexChunk(
        chunk_id="chunk-a-vabc", file_id="file-a", bidder_id="bidder-a", data_role="evidence_material",
        file_name="资料.md", source_path="资料.md", section="参数表", content=content,
        parse_status="parsed", block_kind="table", parse_version="file-a/versions/abc/document.md",
        image_paths=["file-a/versions/abc/images/1.png"],
    )
    original = chunk.model_dump()
    loaded = LoadedIndex(IndexManifest(dimension=2, chunk_count=1, embedding_model="test", embedding_fingerprint="test"),
                         [chunk], np.asarray([[1.0, 0.0]], dtype=np.float32), {})
    seen = []

    def respond(request):
        body = json.loads(request.content)
        texts = body["input"]["documents"]
        seen.extend(texts)
        i = next((i for i, text in enumerate(texts) if "TAIL_MARKER" in text), 0)
        return httpx.Response(200, json={"output": {"results": [{"index": i, "relevance_score": 0.8}]}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        service = RetrievalService(FakeEmbedding(), RerankModel(cfg, cloud.BailianRerankBackend(cfg, client=client)),
                                   settings, indexes={"scope": loaded})
        result = (await service.search("参数", IndexFilter(scope_id="scope", bidder_id="bidder-a",
                   data_role="evidence_material", file_ids=["file-a"])))[0]
    assert result.content == result.source.quote == content
    assert result.chunk_id == chunk.chunk_id and result.parse_version == chunk.parse_version
    assert result.image_paths == chunk.image_paths and result.truncated is False
    assert chunk.model_dump() == original
    assert any("TAIL_MARKER" in text for text in seen)
    assert all(len(text.encode()) <= 1800 and 'rowspan="1"' not in text for text in seen)


@pytest.mark.asyncio
async def test_non_bailian_backend_receives_untouched_html(temp_settings):
    from app.llm.rerank_model import RerankDocument, RerankModel

    text = '<table><tr><td rowspan="1" colspan="2">全文</td></tr></table>'
    class LocalBackend:
        async def score(self, query, documents):
            assert documents[0].text == text
            return [0.5]

    results = await RerankModel(temp_settings.rerank_model, LocalBackend()).rerank(
        "q", [RerankDocument(document_id="local", text=text)], 1
    )
    assert results[0].document_id == "local"


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
