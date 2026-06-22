"""Embedding 顺序、批量、归一化和维度契约测试。"""

import numpy as np
import pytest

from app.llm.embedding_model import EmbeddingModel
from app.llm.errors import ModelCallError


class FakeEmbeddings:
    async def aembed_documents(self, texts):
        return [[float(len(text)), 1.0] for text in texts]


@pytest.mark.asyncio
async def test_embedding_batches_and_normalizes():
    model = EmbeddingModel(FakeEmbeddings(), dimension=2, batch_size=2)

    matrix = await model.embed(["a", "abcd", "xy"])

    assert matrix.shape == (3, 2)
    assert np.allclose(np.linalg.norm(matrix, axis=1), 1)


@pytest.mark.asyncio
async def test_embedding_dimension_mismatch_fails():
    model = EmbeddingModel(FakeEmbeddings(), dimension=3, batch_size=2)

    with pytest.raises(ModelCallError) as error:
        await model.embed(["a"])

    assert error.value.code == "embedding_dimension_mismatch"
