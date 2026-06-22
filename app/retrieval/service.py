"""元数据过滤后的向量、关键词和 Rerank 混合检索入口。"""

from __future__ import annotations

import numpy as np

from app.core.config import AppSettings, get_settings
from app.llm.embedding_model import EmbeddingModel
from app.llm.rerank_model import RerankDocument, RerankModel
from app.retrieval.filters import filter_chunk_indices
from app.retrieval.index import embedding_config_fingerprint
from app.retrieval.keyword import keyword_top_k
from app.retrieval.models import IndexFilter, RecallItem, SearchResult
from app.retrieval.store import load_index


class RetrievalService:
    def __init__(
        self,
        embedding_model: EmbeddingModel,
        rerank_model: RerankModel,
        settings: AppSettings | None = None,
    ) -> None:
        self.embedding_model = embedding_model
        self.rerank_model = rerank_model
        self.settings = settings or get_settings()

    async def search(
        self,
        query: str,
        query_filter: IndexFilter,
        *,
        candidate_top_k: int | None = None,
        final_top_k: int | None = None,
    ) -> list[SearchResult]:
        index = load_index(
            query_filter.scope_id,
            expected_fingerprint=embedding_config_fingerprint(
                self.settings.embedding_model
            ),
        )
        allowed = filter_chunk_indices(index.chunks, query_filter)
        if not allowed:
            return []
        candidate_k = candidate_top_k or self.settings.retrieval.candidate_top_k
        final_k = final_top_k or self.settings.retrieval.final_top_k

        query_vector = (await self.embedding_model.embed([query]))[0]
        semantic_scores = index.vectors[allowed] @ query_vector
        semantic_order = np.argsort(semantic_scores)[::-1][:candidate_k]
        recalls: dict[int, RecallItem] = {}
        for local_index in semantic_order:
            row_index = allowed[int(local_index)]
            recalls[row_index] = RecallItem(
                chunk=index.chunks[row_index],
                semantic_score=float(semantic_scores[int(local_index)]),
            )

        keyword = keyword_top_k(
            query,
            index.chunks,
            index.keyword_index,
            set(allowed),
            candidate_k,
        )
        for row_index, score in keyword:
            current = recalls.get(row_index)
            if current is None:
                recalls[row_index] = RecallItem(
                    chunk=index.chunks[row_index],
                    keyword_score=score,
                )
            else:
                recalls[row_index] = current.model_copy(
                    update={"keyword_score": score}
                )

        merged = sorted(
            recalls.values(),
            key=lambda item: (
                self.settings.retrieval.semantic_weight
                * (item.semantic_score or 0)
                + self.settings.retrieval.keyword_weight
                * (item.keyword_score or 0)
            ),
            reverse=True,
        )
        reranked = await self.rerank_model.rerank(
            query,
            [
                RerankDocument(
                    document_id=item.chunk.chunk_id,
                    text=item.chunk.content,
                )
                for item in merged
            ],
            top_n=final_k,
        )
        by_id = {item.chunk.chunk_id: item for item in merged}
        results: list[SearchResult] = []
        for rank, item in enumerate(reranked):
            recall = by_id[item.document_id]
            fallback_score = 1.0 - rank / max(len(reranked), 1)
            results.append(
                SearchResult(
                    chunk_id=recall.chunk.chunk_id,
                    score=item.score if item.score is not None else fallback_score,
                    source=recall.chunk.source_ref(
                        self.settings.retrieval.max_source_chars
                    ),
                    content=recall.chunk.content[
                        : self.settings.retrieval.max_source_chars
                    ],
                    project_id=recall.chunk.project_id,
                    bidder_id=recall.chunk.bidder_id,
                    task_id=recall.chunk.task_id,
                    evidence_type=recall.chunk.evidence_type,
                )
            )
        return results
