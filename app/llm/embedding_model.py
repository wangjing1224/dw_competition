"""本地/百炼 Embedding 统一批处理、维度校验和 L2 归一化。"""

from collections.abc import Sequence
from typing import Protocol

import numpy as np

from app.llm.errors import ModelCallError


class EmbeddingClient(Protocol):
    async def aembed_documents(self, texts: list[str]) -> list[list[float]]: ...
    async def aembed_query(self, text: str) -> list[float]: ...


class EmbeddingBatchError(ModelCallError):
    """保留失败批次的输入范围，索引层据此补文件/chunk定位，不泄漏原文。"""

    def __init__(self, cause: ModelCallError, start: int, end: int):
        super().__init__(
            cause.code,
            f"{cause.message}；输入批次位置 {start + 1}~{end}（从1开始）",
            retryable=cause.retryable,
        )
        self.start, self.end = start, end


class EmbeddingModel:
    def __init__(
        self,
        client: EmbeddingClient,
        dimension: int,
        batch_size: int,
    ) -> None:
        self.client = client
        self.dimension = dimension
        self.batch_size = batch_size

    async def embed(self, texts: Sequence[str]) -> np.ndarray:
        if not texts:
            return np.empty((0, self.dimension), dtype=np.float32)
        matrices = []
        for start in range(0, len(texts), self.batch_size):
            batch = list(texts[start : start + self.batch_size])
            try:
                vectors = await self.client.aembed_documents(batch)
                # 每批单独校验，避免前批漏项、后批多项恰好抵消导致片段错位。
                matrices.append(self._normalize(vectors, len(batch)))
            except Exception as exc:
                error = (
                    exc
                    if isinstance(exc, ModelCallError)
                    else ModelCallError(
                        "embedding_timeout"
                        if isinstance(exc, TimeoutError)
                        else "embedding_failed",
                        type(exc).__name__,
                        retryable=isinstance(exc, TimeoutError),
                    )
                )
                raise EmbeddingBatchError(error, start, start + len(batch)) from exc
        return np.concatenate(matrices, axis=0)

    async def embed_query(self, query: str) -> np.ndarray:
        """查询与文档使用同一空间；百炼适配器负责声明 query 文本类型。"""
        try:
            vector = await self.client.aembed_query(query)
        except ModelCallError:
            raise
        except TimeoutError as exc:
            raise ModelCallError(
                "embedding_timeout", type(exc).__name__, retryable=True
            ) from exc
        except Exception as exc:
            raise ModelCallError(
                "embedding_query_failed", type(exc).__name__, retryable=False
            ) from exc
        return self._normalize([vector], 1)[0]

    def _normalize(self, vectors, count: int) -> np.ndarray:
        try:
            matrix = np.asarray(vectors, dtype=np.float32)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ModelCallError(
                "invalid_embedding_response", "向量不是合法数值矩阵", retryable=False
            ) from exc
        expected = (count, self.dimension)
        if matrix.shape != expected:
            raise ModelCallError(
                "embedding_dimension_mismatch",
                f"向量矩阵维度为 {matrix.shape}，期望 {expected}",
                retryable=False,
            )
        if not np.isfinite(matrix).all():
            raise ModelCallError(
                "invalid_embedding_response", "向量包含非有限数值", retryable=False
            )
        # 用float64求范数，避免合法float32大数平方时溢出。
        norms = np.linalg.norm(matrix.astype(np.float64), axis=1, keepdims=True)
        if np.any(norms == 0):
            raise ModelCallError(
                "zero_embedding_vector",
                "Embedding 服务返回了零向量",
                retryable=False,
            )
        return (matrix / norms).astype(np.float32)
