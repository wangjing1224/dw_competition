"""百炼原生HTTP契约；全部使用MockTransport，不需要Key、联网或下载本地权重。"""

import asyncio
import json
import logging

import httpx
import numpy as np
import pytest
import pytest_asyncio
from pydantic import SecretStr, ValidationError

from app.core.config import EmbeddingSettings, RerankSettings
from app.llm import bailian_retrieval as cloud
from app.llm.embedding_model import EmbeddingModel
from app.llm.errors import ModelCallError
from app.llm.factory import build_embedding_model
from app.llm.rerank_model import LocalCrossEncoderBackend, RerankDocument, RerankModel
from app.retrieval.index import embedding_config_fingerprint


def embedding_settings(**changes):
    return EmbeddingSettings.model_validate(
        {
            "platform": "bailian",
            "base_url": "https://mock.test/api/v1",
            "api_key": "secret-test-key",
            "model": "qwen3.7-text-embedding",
            "dimension": 256,
            "batch_size": 16,
            "timeout_seconds": 12,
            "max_retries": 2,
            **changes,
        }
    )


def rerank_settings(**changes):
    return RerankSettings.model_validate(
        {
            "platform": "bailian",
            "base_url": "https://mock.test/api/v1",
            "api_key": "secret-test-key",
            "model": "qwen3.7-text-rerank",
            "top_n": 10,
            "instruction": "Rank relevant tender evidence.",
            **changes,
        }
    )


@pytest_asyncio.fixture(autouse=True)
async def release_pools():
    yield
    await cloud.close_retrieval_clients()


def embedding_response(texts, dimension=256):
    return {
        "request_id": "mock-request",
        "usage": {"total_tokens": 20},
        "output": {
            "embeddings": [
                {
                    "text_index": index,
                    "embedding": [float(len(text)), 1.0] + [0.0] * (dimension - 2),
                }
                for index, text in reversed(list(enumerate(texts)))
            ]
        },
    }


@pytest.mark.asyncio
async def test_embedding_native_protocol_batch_query_mapping_normalization():
    requests = []

    def respond(request):
        body = json.loads(request.content)
        requests.append(body)
        assert request.url.path == "/api/v1" + cloud.EMBEDDING_PATH
        assert request.headers["authorization"] == "Bearer secret-test-key"
        assert request.extensions["timeout"]["read"] == 12
        assert body["parameters"]["dimension"] == 256
        assert body["parameters"]["output_type"] == "dense"
        assert "dimensions" not in body and "input" in body
        return httpx.Response(200, json=embedding_response(body["input"]["texts"]))

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        adapter = cloud.BailianEmbeddings(
            embedding_settings(batch_size=2), client=client
        )
        model = EmbeddingModel(adapter, 256, 2)
        matrix = await model.embed(["甲", "乙乙", "丙丙丙", "丁丁丁丁", "戊戊戊戊戊"])
        query = await model.embed_query("甲")
        assert matrix.shape == (5, 256) and matrix.dtype == np.float32
        assert np.allclose(np.linalg.norm(matrix, axis=1), 1.0)
        assert np.allclose(query, matrix[0])
        assert matrix[0, 0] < matrix[1, 0] < matrix[2, 0]
        assert [r["parameters"]["text_type"] for r in requests] == ["document"] * 3 + [
            "query"
        ]
        assert [len(r["input"]["texts"]) for r in requests] == [2, 2, 1, 1]
        assert (await model.embed([])).shape == (0, 256)
        assert await adapter.aembed_documents([]) == []
        assert len(requests) == 4


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "name,limit,dimension",
    [
        ("qwen3.7-text-embedding", 20, 256),
        ("qwen3.7-text-embedding-flash", 20, 256),
        ("text-embedding-v4", 10, 64),
    ],
)
async def test_registered_embedding_batch_boundaries(name, limit, dimension):
    cfg = embedding_settings(model=name, batch_size=limit, dimension=dimension)
    requests = []

    def respond(request):
        body = json.loads(request.content)
        requests.append(body)
        return httpx.Response(
            200, json=embedding_response(body["input"]["texts"], dimension)
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        adapter = cloud.BailianEmbeddings(cfg, client=client)
        assert len(await adapter.aembed_documents(["甲"] * limit)) == limit
        with pytest.raises(ModelCallError, match="批量"):
            await adapter.aembed_documents(["甲"] * (limit + 1))
        with pytest.raises(ModelCallError, match="空文本"):
            await adapter.aembed_query(" ")
        assert len(requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fault",
    [
        "duplicate",
        "missing",
        "out_of_range",
        "string_index",
        "dimension",
        "nan",
        "inf",
        "string_value",
        "zero",
        "not_json",
    ],
)
async def test_invalid_embedding_response_is_not_accepted(fault):
    data = embedding_response(["a", "b"])
    entries = data["output"]["embeddings"]
    if fault == "duplicate":
        entries[0]["text_index"] = entries[1]["text_index"]
    elif fault == "missing":
        entries.pop()
    elif fault == "out_of_range":
        entries[0]["text_index"] = 2
    elif fault == "string_index":
        entries[0]["text_index"] = "1"
    elif fault == "dimension":
        entries[0]["embedding"].pop()
    elif fault in {"nan", "inf", "string_value"}:
        entries[0]["embedding"][0] = "1.0" if fault == "string_value" else float(fault)
    elif fault == "zero":
        entries[0]["embedding"] = [0.0] * 256
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200, text="not json" if fault == "not_json" else json.dumps(data)
            )
        )
    ) as client:
        model = EmbeddingModel(
            cloud.BailianEmbeddings(embedding_settings(), client=client), 256, 16
        )
        with pytest.raises(ModelCallError):
            await model.embed(["a", "b"])


@pytest.mark.parametrize(
    "changes",
    [
        {"model": "text-embedding-v4"},  # default16 exceeds v4's10
        {"batch_size": 21},
        {"dimension": 7},
        {"model": "unregistered"},
        {"platform": "openai"},
        {"max_concurrency": 0},
    ],
)
def test_embedding_capabilities_are_validated(changes):
    with pytest.raises(ValidationError):
        embedding_settings(**changes)


def test_local_alias_does_not_use_cloud_limits_and_factories_are_lazy():
    cfg = embedding_settings(
        platform="local", model="text-embedding-v4", dimension=1024
    )
    assert cfg.batch_size == 16
    local = build_embedding_model(cfg)
    assert local.client.dimensions is None
    remote = build_embedding_model(embedding_settings())
    assert isinstance(remote.client, cloud.BailianEmbeddings)
    cloud_rerank = RerankModel(rerank_settings())
    assert isinstance(cloud_rerank.backend, cloud.BailianRerankBackend)
    assert cloud_rerank.settings.allow_degradation is False
    local_rerank = RerankSettings(
        platform="local",
        model="Qwen/Qwen3-Reranker-0.6B",
        cache_dir="models",
        batch_size=4,
        max_length=2048,
        top_n=10,
        instruction="Rank evidence.",
    )
    assert local_rerank.allow_degradation is True
    assert isinstance(RerankModel(local_rerank).backend, LocalCrossEncoderBackend)


@pytest.mark.parametrize(
    "url",
    [
        "",
        "https://mock.test/compatible-mode/v1",
        "https://mock.test/api/v1/chat/completions",
        "https://mock.test/api/v1?key=secret",
    ],
)
def test_wrong_native_base_url_rejected(url):
    with pytest.raises(ValueError, match="base_url"):
        cloud.BailianEmbeddings(embedding_settings(base_url=url))


def test_missing_key_rejected_without_io():
    with pytest.raises(ValueError, match="API Key"):
        cloud.BailianEmbeddings(embedding_settings(api_key=""))


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [400, 401, 402, 403, 404, 422])
async def test_permanent_errors_no_retry_no_sensitive_logs(status, caplog):
    calls = []
    caplog.set_level(logging.INFO)

    def respond(request):
        calls.append(request)
        return httpx.Response(
            status,
            json={
                "request_id": "req-safe",
                "error": {
                    "code": "AccessDenied",
                    "message": "secret-test-key PRIVATE_DOCUMENT",
                },
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        model = EmbeddingModel(
            cloud.BailianEmbeddings(embedding_settings(), client=client), 256, 16
        )
        with pytest.raises(ModelCallError) as caught:
            await model.embed(["PRIVATE_DOCUMENT"])
    assert len(calls) == 1 and caught.value.retryable is False
    assert (
        "req-safe" in caught.value.message and f"HTTP {status}" in caught.value.message
    )
    assert "1~1" in caught.value.message
    for secret in ("secret-test-key", "PRIVATE_DOCUMENT"):
        assert secret not in caplog.text and secret not in str(caught.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [429, 500, 502, 503, 504, "timeout", "protocol"])
async def test_transient_errors_have_bounded_retries_and_retry_after(
    monkeypatch, status
):
    calls, sleeps = [], []

    async def sleep(delay):
        sleeps.append(delay)

    monkeypatch.setattr(cloud.asyncio, "sleep", sleep)

    def respond(request):
        calls.append(request)
        if len(calls) <= 2:
            if status == "timeout":
                raise httpx.ReadTimeout("test", request=request)
            if status == "protocol":
                raise httpx.RemoteProtocolError("test", request=request)
            return httpx.Response(
                status, headers={"retry-after": "0.01"}, json={"code": "throttled"}
            )
        return httpx.Response(200, json=embedding_response(["test"]))

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        await cloud.BailianEmbeddings(embedding_settings(), client=client).aembed_query(
            "test"
        )
    assert len(calls) == 3 and len(sleeps) == 2
    if isinstance(status, int):
        assert sleeps == [0.01, 0.01]


@pytest.mark.asyncio
async def test_exhausted_retry_budget_and_cancellation(monkeypatch):
    calls = []

    async def sleep(_):
        pass

    monkeypatch.setattr(cloud.asyncio, "sleep", sleep)

    def fail(request):
        calls.append(request)
        return httpx.Response(503)

    async with httpx.AsyncClient(transport=httpx.MockTransport(fail)) as client:
        with pytest.raises(ModelCallError) as caught:
            await cloud.BailianEmbeddings(
                embedding_settings(), client=client
            ).aembed_query("test")
    assert len(calls) == 3 and caught.value.retryable

    def cancel(_):
        raise asyncio.CancelledError()

    async with httpx.AsyncClient(transport=httpx.MockTransport(cancel)) as client:
        with pytest.raises(asyncio.CancelledError):
            await cloud.BailianEmbeddings(
                embedding_settings(), client=client
            ).aembed_query("test")


@pytest.mark.asyncio
async def test_instances_share_concurrency_and_shutdown_closes_owned_client(
    monkeypatch,
):
    active = peak = 0

    async def respond(request):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.01)
        active -= 1
        return httpx.Response(200, json=embedding_response(["test"]))

    client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    monkeypatch.setattr(cloud.httpx, "AsyncClient", lambda **kwargs: client)
    adapters = [cloud.BailianEmbeddings(embedding_settings()) for _ in range(6)]
    await asyncio.gather(*(adapter.aembed_query("test") for adapter in adapters))
    assert peak == 2
    await cloud.close_retrieval_clients()
    assert client.is_closed


@pytest.mark.asyncio
async def test_rerank_all_scores_remapped_to_original_candidate_ids_across_batches():
    sizes = []

    def respond(request):
        body = json.loads(request.content)
        assert request.url.path == "/api/v1" + cloud.RERANK_PATH
        texts = body["input"]["documents"]
        sizes.append(len(texts))
        assert body["parameters"] == {
            "top_n": len(texts),
            "instruct": "Rank relevant tender evidence.",
        }
        assert body["input"]["query"] == "采购"
        return httpx.Response(
            200,
            json={
                "output": {
                    "results": [
                        {"index": i, "relevance_score": int(text) / 1000}
                        for i, text in reversed(list(enumerate(texts)))
                    ]
                }
            },
        )

    documents = [
        RerankDocument(document_id=f"chunk-{i}", text=str(i)) for i in range(501)
    ]
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        backend = cloud.BailianRerankBackend(rerank_settings(), client=client)
        results = await RerankModel(rerank_settings(), backend).rerank(
            "采购", documents, top_n=3
        )
        assert [r.document_id for r in results] == [
            "chunk-500",
            "chunk-499",
            "chunk-498",
        ]
        assert sizes == [500, 1]
        assert await backend.score("采购", []) == []


@pytest.mark.asyncio
async def test_rerank_duplicate_texts_are_not_collapsed_and_estimate_only_batches():
    sizes = []
    long_text = "中" * 25000  # byte估算大于7万，不应假冒精确token上限把整表硬拒绝

    def respond(request):
        texts = json.loads(request.content)["input"]["documents"]
        sizes.append(len(texts))
        assert all(text == long_text for text in texts)
        return httpx.Response(
            200,
            json={
                "output": {
                    "results": [
                        {"index": i, "relevance_score": 0.9} for i in range(len(texts))
                    ]
                }
            },
        )

    docs = [RerankDocument(document_id=str(i), text=long_text) for i in range(3)]
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        backend = cloud.BailianRerankBackend(rerank_settings(), client=client)
        results = await RerankModel(rerank_settings(), backend).rerank("采购", docs)
    assert sizes == [1, 1, 1]
    assert [r.document_id for r in results] == ["0", "1", "2"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "items",
    [
        [],
        [{"index": 1, "relevance_score": 0.8}],
        [{"index": 0, "relevance_score": float("nan")}],
        [{"index": 0, "relevance_score": "0.8"}],
    ],
)
async def test_bad_cloud_rerank_response_no_silent_fallback(items):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200, text=json.dumps({"output": {"results": items}})
            )
        )
    ) as client:
        cfg = rerank_settings()
        model = RerankModel(cfg, cloud.BailianRerankBackend(cfg, client=client))
        with pytest.raises(ModelCallError):
            await model.rerank(
                "query", [RerankDocument(document_id="chunk-1", text="one")]
            )


def test_fingerprint_excludes_credentials_retry_batch_and_concurrency():
    cfg = embedding_settings()
    original = embedding_config_fingerprint(cfg)
    assert (
        embedding_config_fingerprint(
            cfg.model_copy(
                update={
                    "api_key": SecretStr("different"),
                    "batch_size": 8,
                    "max_concurrency": 4,
                    "timeout_seconds": 8,
                    "max_retries": 0,
                }
            )
        )
        == original
    )
    for changes in (
        {"platform": "local"},
        {"model": "qwen3.7-text-embedding-flash"},
        {"dimension": 512},
        {"base_url": "https://other.test/api/v1"},
    ):
        assert embedding_config_fingerprint(cfg.model_copy(update=changes)) != original


@pytest.mark.asyncio
async def test_full_cloud_index_search_and_failed_rebuild_leave_index_intact(
    monkeypatch, temp_settings
):
    from app.models.documents import DocumentRef, ParsedDocument, ParsedSection
    from app.retrieval.index import build_document_index
    from app.retrieval.models import IndexFilter, IndexScope
    from app.retrieval.service import RetrievalService
    from app.retrieval.store import IndexRebuildRequired, index_directory, load_index

    cfg = embedding_settings()
    settings = temp_settings.model_copy(
        update={"embedding_model": cfg, "rerank_model": rerank_settings()}
    )
    monkeypatch.setattr("app.retrieval.store.get_settings", lambda: settings)
    docs = [
        DocumentRef(
            file_id=f"file-{i}",
            file_name=f"招标{i}.md",
            source_path=f"招标{i}.md",
            project_id="p1",
            data_role="rule_source",
            parse_status="parsed",
        )
        for i in (1, 2)
    ]
    parsed = {
        doc.file_id: ParsedDocument(
            document=doc,
            parse_status="parsed",
            sections=[
                ParsedSection(
                    section_id="section-1",
                    title="有效期",
                    content=f"文件{doc.file_id}：投标有效期至少90日",
                )
            ],
        )
        for doc in docs
    }
    monkeypatch.setattr("app.retrieval.index.load_parsed_document", parsed.__getitem__)
    scope = IndexScope(scope_id="rules-p1", data_role="rule_source", project_id="p1")
    calls, should_fail = [], False

    def respond(request):
        body = json.loads(request.content)
        calls.append(body)
        if should_fail:
            return httpx.Response(
                400, json={"code": "InputTooLong", "request_id": "request-long"}
            )
        if request.url.path.endswith(cloud.EMBEDDING_PATH):
            return httpx.Response(200, json=embedding_response(body["input"]["texts"]))
        return httpx.Response(
            200,
            json={
                "output": {
                    "results": [
                        {"index": i, "relevance_score": 0.9}
                        for i in range(len(body["input"]["documents"]))
                    ]
                }
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        embeddings = EmbeddingModel(
            cloud.BailianEmbeddings(cfg, client=client), 256, 16
        )
        rerank = RerankModel(
            settings.rerank_model,
            cloud.BailianRerankBackend(settings.rerank_model, client=client),
        )
        manifest = await build_document_index(scope, docs, embeddings, settings)
        assert manifest.chunk_count == 2 and manifest.dimension == 256
        loaded = load_index(
            scope.scope_id, expected_fingerprint=embedding_config_fingerprint(cfg)
        )
        assert loaded.vectors.shape == (2, 256)
        service = RetrievalService(embeddings, rerank, settings)
        results = await service.search(
            "有效期",
            IndexFilter(
                scope_id=scope.scope_id,
                project_id="p1",
                data_role="rule_source",
                file_ids=["file-2"],
            ),
        )
        assert len(results) == 1 and results[0].source.file_id == "file-2"
        assert results[0].chunk_id == loaded.chunks[1].chunk_id
        assert "90日" in results[0].content
        assert calls[0]["parameters"]["text_type"] == "document"
        assert calls[1]["parameters"]["text_type"] == "query"
        with pytest.raises(IndexRebuildRequired):
            load_index(
                scope.scope_id,
                expected_fingerprint=embedding_config_fingerprint(
                    cfg.model_copy(update={"platform": "local"})
                ),
            )
        directory = index_directory(scope.scope_id)
        before = {p.name: p.read_bytes() for p in directory.iterdir()}
        should_fail = True
        with pytest.raises(ModelCallError) as caught:
            await build_document_index(scope, docs, embeddings, settings)
        assert "file-1/" + loaded.chunks[0].chunk_id in caught.value.message
        assert (
            "InputTooLong" in caught.value.message
            and "request-long" in caught.value.message
        )
        assert before == {p.name: p.read_bytes() for p in directory.iterdir()}
        fresh = scope.model_copy(update={"scope_id": "rules-new"})
        with pytest.raises(ModelCallError):
            await build_document_index(fresh, docs, embeddings, settings)
        assert not index_directory(fresh.scope_id).exists()
