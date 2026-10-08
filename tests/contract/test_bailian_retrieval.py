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
async def test_rerank_rolling_top_k_remaps_original_ids_without_merging_scores():
    sizes = []

    def respond(request):
        body = json.loads(request.content)
        assert request.url.path == "/api/v1" + cloud.RERANK_PATH
        texts = body["input"]["documents"]
        sizes.append(len(texts))
        assert body["parameters"] == {
            "top_n": 3,
            "instruct": "Rank relevant tender evidence.",
        }
        assert body["input"]["query"] == "采购"
        return httpx.Response(
            200,
            json={
                "output": {
                    "results": [
                        {"index": i, "relevance_score": int(text) / 1000 - len(sizes)}
                        for i, text in sorted(enumerate(texts), key=lambda item: int(item[1]), reverse=True)[:3]
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
        assert sizes == [500, 4]
        assert results[0].score == 0.5 - 2  # 只用最后同一批的分数，不能沿用前批较高分。
        assert await backend.score("采购", []) == []


@pytest.mark.asyncio
async def test_rerank_duplicate_texts_keep_unique_ids_and_window_all_content():
    sizes = []
    long_text = "中" * 25000 + "尾部"

    def respond(request):
        body = json.loads(request.content)
        texts = body["input"]["documents"]
        sizes.append(len(texts))
        assert all(len(text.encode()) <= cloud.RERANK_DOCUMENT_TARGET for text in texts)
        return httpx.Response(
            200,
            json={
                "output": {
                    "results": [
                        {"index": i, "relevance_score": 0.9 if "尾部" in texts[i] else 0.1}
                        for i in sorted(range(len(texts)), key=lambda i: "尾部" not in texts[i])[:body["parameters"]["top_n"]]
                    ]
                }
            },
        )

    docs = [RerankDocument(document_id=str(i), text=long_text) for i in range(3)]
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        backend = cloud.BailianRerankBackend(rerank_settings(), client=client)
        results = await RerankModel(rerank_settings(), backend).rerank("采购", docs)
    assert sizes == [4, 4, 4, 3]  # 三条分别选代表，最后合并原候选，不按内容去重。
    assert [r.document_id for r in results] == ["0", "1", "2"]
    assert all(doc.text == long_text for doc in docs)


@pytest.mark.asyncio
@pytest.mark.parametrize("count,k,capacity,batches", [
    (20, 10, 12, [12] * 5),
    (20, 10, 11, [11] * 10),
    (30, 10, 25, [25, 15]),
    (8, 10, 12, [8]),
    (0, 10, 12, []),
])
async def test_rolling_top_k_fills_budget_consumes_candidates_and_terminates(
    monkeypatch, count, k, capacity, batches
):
    cfg = rerank_settings(instruction="i")
    # 每条100字节，query/instruct各1字节；按字节而非固定候选减K。
    monkeypatch.setattr(cloud, "RERANK_BATCH_TOKEN_TARGET", capacity * 102)
    observed, seen = [], set()

    def respond(request):
        body = json.loads(request.content)
        texts, limit = body["input"]["documents"], body["parameters"]["top_n"]
        values = [int(text[:3]) for text in texts]
        observed.append(len(texts))
        seen.update(values)
        assert sum(len(t.encode()) + 2 for t in texts) <= capacity * 102
        order = sorted(range(len(texts)), key=lambda i: values[i], reverse=True)[:limit]
        return httpx.Response(200, json={"output": {"results": [
            {"index": i, "relevance_score": values[i] - len(observed) * 100}
            for i in order
        ]}})

    docs = [RerankDocument(document_id=f"id-{i}", text=str(i).zfill(3).ljust(100, "x")) for i in range(count)]
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        backend = cloud.BailianRerankBackend(cfg, client=client)
        results = await RerankModel(cfg, backend).rerank("q", docs, top_n=k)
    assert observed == batches
    assert seen == set(range(count))
    assert [r.document_id for r in results] == [f"id-{i}" for i in reversed(range(max(count-k, 0), count))]
    assert all(r.score == int(r.document_id[3:]) - len(batches) * 100 for r in results)


def test_scoring_cleanup_is_structural_and_preserves_real_merges_nested_tables():
    original = (
        '对象a rowspan="1" ≤ 2 kg\n<table><tr><td rowspan="01" colspan="1">'
        '<table><tr><td colspan="2">≥3 mg</td></tr></table>'
        '</td><td rowspan="2">0.25%</td></tr><tr><td>尾</td></tr></table>'
    )
    cleaned = cloud._clean_scoring_text(original)
    assert '对象a rowspan="1" ≤ 2 kg' in cleaned
    assert 'rowspan="01"' not in cleaned and 'colspan="1"' not in cleaned
    assert 'colspan="2"' in cleaned and 'rowspan="2"' in cleaned
    assert cleaned.count("<table>") == 2 and "≥3 mg" in cleaned and "0.25%" in cleaned
    plain = '普通文本 rowspan="1"; colspan="1"; 1 < 2'
    assert cloud._clean_scoring_text(plain) == plain


def test_table_windows_retain_headers_units_tail_and_spanning_context():
    rows = ['<tr><th>对象</th><th>数值（mg）</th></tr>']
    for i in range(40):
        rows.append(f'<tr><td rowspan="2">对象-{i}</td><td>数值-{i} ≤ 3</td></tr>')
        rows.append(f'<tr><td>后行-{i} ≥ 2</td></tr>')
    original = '采购清单\n<table>' + ''.join(rows) + '</table>'
    windows = cloud._scoring_windows(original, 1800)
    assert len(windows) > 2
    assert all(len(w.encode()) <= 1800 for w in windows)
    assert all("采购清单" in w and "数值（mg）" in w for w in windows)
    for i in range(40):
        assert any(f"数值-{i} ≤ 3" in w for w in windows)
        assert any(f"后行-{i} ≥ 2" in w and f"对象-{i}" in w for w in windows)
    assert "后行-39" in windows[-1]


def test_huge_row_cell_nested_table_and_plaintext_are_windowed_without_dropping_tail():
    cell = "连续说明。" * 2000 + "最终条件 ≥ 22 kg"
    table = (
        '<table><tr><th>编号</th><th>记录内容（kg）</th></tr>'
        '<tr><td>实体A</td><td>' + cell +
        '<table><tr><th>子项</th><th>单位</th></tr><tr><td>嵌套末行</td><td>mg</td></tr></table>'
        '</td></tr></table>'
    )
    windows = cloud._scoring_windows(table, 2000)
    assert len(windows) > 2 and all(len(w.encode()) <= 2000 for w in windows)
    assert any("最终条件 ≥ 22 kg" in w for w in windows)
    assert any("嵌套末行" in w and "子项" in w and "实体A" in w for w in windows)
    assert all("行2 / 列2" in w for w in windows if "连续说明" in w)
    text = "无分隔符" * 3000 + "TAIL"
    pieces = cloud._scoring_windows(text, 800)
    assert "".join(w.split('\n', 1)[1] for w in pieces) == text


def test_row_headers_are_not_mistaken_for_a_huge_repeated_column_header():
    table = '<table><tr><th>名称</th><th>值</th></tr>' + ''.join(
        f'<tr><th>行对象-{i}</th><td>数据-{i}</td></tr>' for i in range(100)
    ) + '</table>'
    windows = cloud._scoring_windows(table, 1500)
    assert len(windows) > 1 and all(len(w.encode()) <= 1500 for w in windows)
    for i in range(100):
        assert any(f"行对象-{i}" in w and f"数据-{i}" in w for w in windows)


@pytest.mark.asyncio
async def test_windows_use_rolling_k_one_then_compare_originals(monkeypatch):
    cfg = rerank_settings(instruction="i")
    monkeypatch.setattr(cloud, "RERANK_DOCUMENT_TARGET", 400)
    monkeypatch.setattr(cloud, "RERANK_BATCH_TOKEN_TARGET", 850)
    requests = []

    def respond(request):
        body = json.loads(request.content)
        texts = body["input"]["documents"]
        requests.append(texts)
        limit = body["parameters"]["top_n"]
        order = sorted(range(len(texts)), key=lambda i: "TAIL_TARGET" not in texts[i])[:limit]
        return httpx.Response(200, json={"output": {"results": [
            {"index": i, "relevance_score": (1 if "TAIL_TARGET" in texts[i] else 0) - len(requests)}
            for i in order
        ]}})

    docs = [RerankDocument(document_id="long", text="正文" * 1500 + " TAIL_TARGET"),
            RerankDocument(document_id="other", text="irrelevant")]
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        results = await RerankModel(cfg, cloud.BailianRerankBackend(cfg, client=client)).rerank("q", docs, 1)
    assert len(requests) > 3
    assert all(len(batch) >= 2 for batch in requests)
    assert any("TAIL_TARGET" in t for t in requests[-1])
    assert results[0].document_id == "long"
    assert results[0].score == 1 - len(requests)


@pytest.mark.asyncio
async def test_query_and_unshrinkable_context_fail_before_http(monkeypatch):
    cfg = rerank_settings(instruction="i")
    calls = []
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req: calls.append(req))) as client:
        model = RerankModel(cfg, cloud.BailianRerankBackend(cfg, client=client))
        with pytest.raises(ModelCallError, match="未截断查询"):
            await model.rerank("q" * 4001, [RerankDocument(document_id="x", text="ok")])
        monkeypatch.setattr(cloud, "RERANK_DOCUMENT_TARGET", 200)
        text = '<table><tr><th>' + '长必要表头' * 100 + '</th></tr><tr><td>数据</td></tr></table>'
        with pytest.raises(ModelCallError, match="必要表头"):
            await model.rerank("q", [RerankDocument(document_id="x", text=text)])
    assert calls == []


@pytest.mark.asyncio
async def test_single_document_target_reserves_query_and_instruction():
    query = "q" * 4000
    cfg = rerank_settings(instruction="i" * 2000)
    original = "正文" * 6000 + "TAIL"
    seen = []

    def respond(request):
        body = json.loads(request.content)
        texts = body["input"]["documents"]
        seen.extend(texts)
        assert all(len(text.encode()) + 6000 <= cloud.RERANK_DOCUMENT_TARGET for text in texts)
        return httpx.Response(200, json={"output": {"results": [
            {"index": 0, "relevance_score": 1.0}
        ]}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        backend = cloud.BailianRerankBackend(cfg, client=client)
        result = await backend.select_top_k(
            query, [RerankDocument(document_id="long", text=original)], 1
        )
    assert result == [("long", 1.0)]
    assert any("TAIL" in text for text in seen)


@pytest.mark.asyncio
async def test_shrink_all_representations_before_first_comparison(monkeypatch):
    cfg = rerank_settings(instruction="i")
    monkeypatch.setattr(cloud, "RERANK_BATCH_TOKEN_TARGET", 2200)
    calls = []

    def respond(request):
        body = json.loads(request.content)
        texts, k = body["input"]["documents"], body["parameters"]["top_n"]
        calls.append((texts, k))
        assert sum(len(text.encode()) + 2 for text in texts) <= 2200
        return httpx.Response(200, json={"output": {"results": [
            {"index": i, "relevance_score": 1 / (i + 1)} for i in range(k)
        ]}})

    docs = [RerankDocument(document_id=f"id-{i}", text=f"object{i} " + "value " * 200) for i in range(20)]
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        result = await RerankModel(cfg, cloud.BailianRerankBackend(cfg, client=client)).rerank("q", docs, 10)
    assert len(result) == len({r.document_id for r in result}) == 10
    # 先处理全部20个候选，再进入候选间比较；没有比较后缩窗而忘记已淘汰候选。
    first_comparison = next(i for i, (_, k) in enumerate(calls) if k == 10)
    assert first_comparison >= 20
    assert all(k == 10 for _, k in calls[first_comparison:])
    assert all(len(texts) > 10 for texts, k in calls[first_comparison:])


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["duplicate", "out_of_range", "missing", "nan", "bool_index"])
async def test_top_k_response_validation_uses_full_batch_index_range(fault):
    items = [{"index": 2, "relevance_score": 0.9}, {"index": 0, "relevance_score": 0.8}]
    if fault == "duplicate":
        items[1]["index"] = 2
    elif fault == "out_of_range":
        items[0]["index"] = 3
    elif fault == "missing":
        items.pop()
    elif fault == "nan":
        items[0]["relevance_score"] = float("nan")
    else:
        items[0]["index"] = True
    cfg = rerank_settings()
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda _: httpx.Response(200, text=json.dumps({"output": {"results": items}}))
    )) as client:
        with pytest.raises(ModelCallError):
            await RerankModel(cfg, cloud.BailianRerankBackend(cfg, client=client)).rerank(
                "q", [RerankDocument(document_id=str(i), text=str(i)) for i in range(3)], 2
            )


def test_invalid_parameter_does_not_automatically_mean_length_error():
    assert "constraint=unknown" in cloud._safe_length_diagnostic({
        "code": "InvalidParameter", "message": "invalid model or parameter PRIVATE_TEXT"
    })
    diagnostic = cloud._safe_length_diagnostic({"error": {
        "message": "query token length exceeds maximum PRIVATE_TEXT", "input_tokens": 4100,
        "max_tokens": 4000, "document": "PRIVATE_TEXT", "api_key": "secret",
    }})
    assert "constraint=query_length" in diagnostic and "4100" in diagnostic
    assert "PRIVATE_TEXT" not in diagnostic and "secret" not in diagnostic


@pytest.mark.asyncio
@pytest.mark.parametrize("degrade", [False, True])
async def test_mid_rolling_failure_never_returns_partial_top_k(monkeypatch, degrade, caplog):
    cfg = rerank_settings(instruction="i", allow_degradation=degrade)
    monkeypatch.setattr(cloud, "RERANK_MAX_ITEMS", 3)
    count = 0
    caplog.set_level(logging.INFO)

    def respond(request):
        nonlocal count
        count += 1
        if count == 2:
            return httpx.Response(400, json={"request_id": "len-safe", "code": "InvalidParameter",
                "message": "document token length exceeds maximum PRIVATE_DOC secret-test-key"})
        return httpx.Response(200, json={"output": {"results": [{"index": 2, "relevance_score": 0.9}]}})

    docs = [RerankDocument(document_id=f"id-{i}", text=f"PRIVATE_DOC {i}") for i in range(5)]
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        model = RerankModel(cfg, cloud.BailianRerankBackend(cfg, client=client))
        if degrade:
            result = await model.rerank("q", docs, 1)
            assert [(r.document_id, r.score) for r in result] == [("id-0", None)]
        else:
            with pytest.raises(ModelCallError, match="constraint=document_length") as caught:
                await model.rerank("q", docs, 1)
            assert "PRIVATE_DOC" not in str(caught.value)
    assert count == 2
    assert "PRIVATE_DOC" not in caplog.text and "secret-test-key" not in caplog.text
    assert "utf8_bytes_proxy" in caplog.text


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
