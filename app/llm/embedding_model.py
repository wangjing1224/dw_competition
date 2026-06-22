"""LangChain Embedding 批处理、维度校验和 L2 归一化。"""

from collections.abc import Sequence

import numpy as np
from langchain_core.embeddings import Embeddings

from app.llm.errors import ModelCallError


class EmbeddingModel:
    def __init__(
        self,
        client: Embeddings,
        dimension: int,
        batch_size: int,
    ) -> None:
        self.client = client
        self.dimension = dimension
        self.batch_size = batch_size

    async def embed(self, texts: Sequence[str]) -> np.ndarray:
        if not texts:
            return np.empty((0, self.dimension), dtype=np.float32)
        vectors: list[list[float]] = []
        try:
            for start in range(0, len(texts), self.batch_size):
                batch = list(texts[start : start + self.batch_size])
                vectors.extend(await self.client.aembed_documents(batch))
        except TimeoutError as exc:
            raise ModelCallError(
                "embedding_timeout", str(exc), retryable=True
            ) from exc
        except Exception as exc:
            raise ModelCallError(
                "embedding_failed", str(exc), retryable=False
            ) from exc

        matrix = np.asarray(vectors, dtype=np.float32)
        expected = (len(texts), self.dimension)
        if matrix.shape != expected:
            raise ModelCallError(
                "embedding_dimension_mismatch",
                f"向量矩阵维度为 {matrix.shape}，期望 {expected}",
                retryable=False,
            )
        norms = np.linalg.norm(matrix, axis=1, keepdims=True)
        if np.any(norms == 0):
            raise ModelCallError(
                "zero_embedding_vector",
                "Embedding 服务返回了零向量",
                retryable=False,
            )
        return matrix / norms
