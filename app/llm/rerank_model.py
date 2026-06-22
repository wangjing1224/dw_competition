"""进程内 Qwen3 Reranker 懒加载、设备选择、稳定 ID 排序和降级。"""

from __future__ import annotations

import asyncio
import logging
import threading
from pathlib import Path
from typing import Any, ClassVar, Protocol

from pydantic import BaseModel, ConfigDict

from app.core.config import RerankSettings
from app.llm.errors import ModelCallError


logger = logging.getLogger(__name__)


class RerankDocument(BaseModel):
    """送入 Rerank 的片段；document_id 用于排序后精确回链。"""

    model_config = ConfigDict(extra="forbid")

    document_id: str
    text: str


class RerankResult(BaseModel):
    """Rerank 排序结果；降级时 score 为 None。"""

    model_config = ConfigDict(extra="forbid")

    document_id: str
    score: float | None = None


class RerankBackend(Protocol):
    async def score(
        self, query: str, documents: list[RerankDocument]
    ) -> list[float]: ...


def _resolve_device(requested: str, torch_module: Any) -> str:
    """选择当前 PyTorch 运行时真正可用的设备。"""

    if requested != "auto":
        return requested

    # 昇腾安装 torch_npu 后会提供 torch.npu；其他国产卡需要各自适配层，
    # 可通过配置明确填写其 PyTorch device 字符串。
    npu = getattr(torch_module, "npu", None)
    if npu is not None and callable(getattr(npu, "is_available", None)):
        if npu.is_available():
            return "npu"

    if torch_module.cuda.is_available():
        return "cuda"

    mps = getattr(torch_module.backends, "mps", None)
    if mps is not None and mps.is_available():
        return "mps"

    return "cpu"


def _resolve_dtype(
    requested: str,
    device: str,
    torch_module: Any,
) -> Any:
    """CPU 默认 float32；加速设备默认 float16，优先保证兼容性。"""

    dtype_name = requested
    if dtype_name == "auto":
        dtype_name = "float32" if device == "cpu" else "float16"
    return getattr(torch_module, dtype_name)


class LocalCrossEncoderBackend:
    """使用 sentence-transformers CrossEncoder 在当前进程执行 Rerank。"""

    # 相同配置在一个进程中共享模型，防止并发审核任务重复占用内存。
    _models: ClassVar[dict[tuple[str, str, str, int, bool], Any]] = {}
    _model_locks: ClassVar[
        dict[tuple[str, str, str, int, bool], threading.Lock]
    ] = {}
    _registry_lock: ClassVar[threading.Lock] = threading.Lock()

    def __init__(self, settings: RerankSettings) -> None:
        self.settings = settings
        self._resolved_device: str | None = None
        self._resolved_model_path: str | None = None

    def _resolve_model_path(self) -> str:
        """将魔塔模型 ID 下载为本地目录，或校验显式本地目录。"""

        if self._resolved_model_path is not None:
            return self._resolved_model_path

        configured_path = Path(self.settings.model).expanduser()
        if configured_path.exists():
            self._resolved_model_path = str(configured_path.resolve())
            return self._resolved_model_path

        if self.settings.source == "local":
            raise ModelCallError(
                "rerank_model_not_found",
                f"本地 Rerank 模型目录不存在: {configured_path}",
                retryable=False,
            )

        try:
            from modelscope import snapshot_download
        except ImportError as exc:
            raise ModelCallError(
                "rerank_dependency_missing",
                "从魔塔下载 Rerank 模型需要安装 modelscope",
                retryable=False,
            ) from exc

        try:
            self._resolved_model_path = snapshot_download(
                self.settings.model,
                cache_dir=str(self.settings.cache_dir),
                local_files_only=self.settings.local_files_only,
            )
        except Exception as exc:
            raise ModelCallError(
                "rerank_model_download_failed",
                f"魔塔 Rerank 模型下载失败: {exc}",
                retryable=False,
            ) from exc
        return self._resolved_model_path

    def _cache_key(
        self,
        device: str,
        dtype_name: str,
    ) -> tuple[str, str, str, int, bool]:
        return (
            self.settings.model,
            device,
            dtype_name,
            self.settings.max_length,
            self.settings.local_files_only,
        )

    def _model_lock(
        self,
        key: tuple[str, str, str, int, bool],
    ) -> threading.Lock:
        with self._registry_lock:
            return self._model_locks.setdefault(key, threading.Lock())

    def _load_model(
        self,
        device: str,
        dtype_name: str,
        torch_module: Any,
        cross_encoder_type: Any,
    ) -> Any:
        key = self._cache_key(device, dtype_name)
        cached = self._models.get(key)
        if cached is not None:
            return cached

        with self._model_lock(key):
            cached = self._models.get(key)
            if cached is not None:
                return cached

            dtype = _resolve_dtype(dtype_name, device, torch_module)
            logger.info(
                "loading local rerank model",
                extra={
                    "model": self.settings.model,
                    "device": device,
                    "dtype": str(dtype),
                },
            )
            try:
                model_path = self._resolve_model_path()
                model = cross_encoder_type(
                    model_path,
                    device=device,
                    max_length=self.settings.max_length,
                    model_kwargs={"torch_dtype": dtype},
                    prompts={"tender_review": self.settings.instruction},
                    default_prompt_name="tender_review",
                    local_files_only=self.settings.local_files_only,
                )
            except Exception as exc:
                raise ModelCallError(
                    "rerank_model_load_failed",
                    f"本地 Rerank 模型加载失败: {exc}",
                    retryable=False,
                ) from exc

            self._resolved_device = device
            self._models[key] = model
            return model

    def _score_on_device(
        self,
        query: str,
        documents: list[RerankDocument],
        *,
        device: str,
        dtype_name: str,
        torch_module: Any,
        cross_encoder_type: Any,
    ) -> list[float]:
        model = self._load_model(
            device,
            dtype_name,
            torch_module,
            cross_encoder_type,
        )
        pairs = [(query, document.text) for document in documents]
        try:
            scores = model.predict(
                pairs,
                batch_size=self.settings.batch_size,
                show_progress_bar=False,
                convert_to_numpy=True,
            )
            return [float(score) for score in scores]
        except Exception as exc:
            raise ModelCallError(
                "rerank_inference_failed",
                f"本地 Rerank 推理失败: {exc}",
                retryable=False,
            ) from exc

    def _score_sync(
        self,
        query: str,
        documents: list[RerankDocument],
    ) -> list[float]:
        try:
            import torch
            from sentence_transformers import CrossEncoder
        except ImportError as exc:
            raise ModelCallError(
                "rerank_dependency_missing",
                "本地 Rerank 需要安装 torch 和 sentence-transformers",
                retryable=False,
            ) from exc

        device = _resolve_device(self.settings.device, torch)
        dtype_name = self.settings.dtype
        try:
            return self._score_on_device(
                query,
                documents,
                device=device,
                dtype_name=dtype_name,
                torch_module=torch,
                cross_encoder_type=CrossEncoder,
            )
        except ModelCallError:
            if device == "cpu":
                raise
            # Mac MPS、CUDA 或国产卡 PyTorch 适配不完整时，真正改用 CPU
            # 重新加载并推理；只有 CPU 也失败后才交给上层执行排序降级。
            logger.warning(
                "rerank accelerator failed, retrying on cpu",
                extra={"failed_device": device},
            )
            return self._score_on_device(
                query,
                documents,
                device="cpu",
                dtype_name="float32",
                torch_module=torch,
                cross_encoder_type=CrossEncoder,
            )

    async def score(
        self,
        query: str,
        documents: list[RerankDocument],
    ) -> list[float]:
        # 模型加载和推理是同步计算，放入工作线程避免阻塞 FastAPI 事件循环。
        return await asyncio.to_thread(self._score_sync, query, documents)


class RerankModel:
    """统一排序入口；模型不可用时按配置保留初召回顺序。"""

    def __init__(
        self,
        settings: RerankSettings,
        backend: RerankBackend | None = None,
    ) -> None:
        self.settings = settings
        self.backend = backend or LocalCrossEncoderBackend(settings)

    async def rerank(
        self,
        query: str,
        documents: list[RerankDocument],
        top_n: int | None = None,
    ) -> list[RerankResult]:
        if not documents:
            return []
        limit = min(top_n or self.settings.top_n, len(documents))
        try:
            scores = await self.backend.score(query, documents)
            if len(scores) != len(documents):
                raise ModelCallError(
                    "invalid_rerank_response",
                    "Rerank 分数数量与文档数量不一致",
                    retryable=False,
                )
            ranked = sorted(
                zip(documents, scores, strict=True),
                key=lambda item: item[1],
                reverse=True,
            )
            return [
                RerankResult(document_id=document.document_id, score=score)
                for document, score in ranked[:limit]
            ]
        except Exception as exc:
            if self.settings.allow_degradation:
                logger.warning("rerank degraded to recall order: %s", exc)
                return [
                    RerankResult(document_id=document.document_id, score=None)
                    for document in documents[:limit]
                ]
            if isinstance(exc, ModelCallError):
                raise
            raise ModelCallError(
                "rerank_failed", str(exc), retryable=False
            ) from exc
