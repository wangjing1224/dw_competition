"""Rerank 稳定 ID、排序和降级契约测试。"""

import sys
from types import ModuleType

import pytest

from app.core.config import RerankSettings
from app.llm.errors import ModelCallError
from app.llm.rerank_model import (
    LocalCrossEncoderBackend,
    RerankDocument,
    RerankModel,
    _resolve_device,
)


class FakeBackend:
    async def score(self, query, documents):
        return [0.2, 0.9]


class FailedBackend:
    async def score(self, query, documents):
        raise TimeoutError("timeout")


class DeviceBackend:
    """模拟 torch 的设备可用性接口，避免测试依赖真实硬件。"""

    def __init__(self, *, npu=False, cuda=False, mps=False):
        self.npu = Availability(npu)
        self.cuda = Availability(cuda)
        self.backends = Backends(mps)


class Availability:
    def __init__(self, available):
        self.available = available

    def is_available(self):
        return self.available


class Backends:
    def __init__(self, mps):
        self.mps = Availability(mps)


def settings(allow_degradation=True, cache_dir="/tmp/dw-models"):
    return RerankSettings(
        model="Qwen/Qwen3-Reranker-0.6B",
        source="modelscope",
        cache_dir=cache_dir,
        device="cpu",
        dtype="float32",
        batch_size=2,
        max_length=512,
        top_n=2,
        instruction="Rank tender review evidence.",
        local_files_only=True,
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


@pytest.mark.parametrize(
    ("torch_backend", "expected"),
    [
        (DeviceBackend(npu=True, cuda=True, mps=True), "npu"),
        (DeviceBackend(cuda=True, mps=True), "cuda"),
        (DeviceBackend(mps=True), "mps"),
        (DeviceBackend(), "cpu"),
    ],
)
def test_auto_device_selection_uses_available_accelerator(
    torch_backend,
    expected,
):
    assert _resolve_device("auto", torch_backend) == expected


def test_explicit_device_is_not_overridden():
    assert _resolve_device("cpu", DeviceBackend(mps=True)) == "cpu"


def test_accelerator_failure_retries_on_cpu(monkeypatch):
    """MPS/CUDA/NPU 失败后应使用 CPU + float32 重新推理。"""

    fake_torch = DeviceBackend(mps=True)
    fake_torch.float16 = object()
    fake_torch.float32 = object()
    fake_torch_module = ModuleType("torch")
    fake_torch_module.npu = fake_torch.npu
    fake_torch_module.cuda = fake_torch.cuda
    fake_torch_module.backends = fake_torch.backends
    fake_torch_module.float16 = fake_torch.float16
    fake_torch_module.float32 = fake_torch.float32

    fake_sentence_transformers = ModuleType("sentence_transformers")
    fake_sentence_transformers.CrossEncoder = object
    monkeypatch.setitem(sys.modules, "torch", fake_torch_module)
    monkeypatch.setitem(
        sys.modules,
        "sentence_transformers",
        fake_sentence_transformers,
    )

    calls = []

    class RecordingBackend(LocalCrossEncoderBackend):
        def _score_on_device(self, query, documents, **kwargs):
            calls.append((kwargs["device"], kwargs["dtype_name"]))
            if kwargs["device"] == "mps":
                raise ModelCallError(
                    "rerank_inference_failed",
                    "simulated mps failure",
                    retryable=False,
                )
            return [0.9]

    backend_settings = settings().model_copy(
        update={"device": "auto", "dtype": "auto"}
    )
    backend = RecordingBackend(backend_settings)
    scores = backend._score_sync(
        "query",
        [RerankDocument(document_id="chunk-a", text="document")],
    )

    assert scores == [0.9]
    assert calls == [("mps", "auto"), ("cpu", "float32")]


def test_modelscope_model_id_resolves_to_local_directory(monkeypatch):
    """魔塔模型 ID 下载后应把本地目录交给 CrossEncoder。"""

    fake_modelscope = ModuleType("modelscope")
    calls = []

    def fake_snapshot_download(model_id, **kwargs):
        calls.append((model_id, kwargs))
        return "/tmp/modelscope/qwen-reranker"

    fake_modelscope.snapshot_download = fake_snapshot_download
    monkeypatch.setitem(sys.modules, "modelscope", fake_modelscope)

    backend = LocalCrossEncoderBackend(settings())

    assert backend._resolve_model_path() == "/tmp/modelscope/qwen-reranker"
    assert calls == [
        (
            "Qwen/Qwen3-Reranker-0.6B",
            {
                "cache_dir": "/tmp/dw-models",
                "local_files_only": True,
            },
        )
    ]
    # 第二次调用直接复用已解析目录，不重复检查远端。
    assert backend._resolve_model_path() == "/tmp/modelscope/qwen-reranker"
    assert len(calls) == 1


def test_local_source_requires_existing_directory(tmp_path):
    local_settings = settings().model_copy(
        update={
            "model": str(tmp_path / "missing-reranker"),
            "source": "local",
        }
    )
    backend = LocalCrossEncoderBackend(local_settings)

    with pytest.raises(ModelCallError) as error:
        backend._resolve_model_path()

    assert error.value.code == "rerank_model_not_found"
