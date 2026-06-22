"""统一 Rerank 后端接口、稳定 ID 排序和显式降级。"""

from __future__ import annotations

from typing import Protocol

import httpx
from pydantic import BaseModel, ConfigDict

from app.core.config import RerankSettings
from app.llm.errors import ModelCallError


class RerankDocument(BaseModel):
    model_config = ConfigDict(extra="forbid")

    document_id: str
    text: str


class RerankResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    document_id: str
    score: float | None = None


class RerankBackend(Protocol):
    async def score(
        self, query: str, documents: list[RerankDocument]
    ) -> list[float]: ...


class HttpRerankBackend:
    """调用常见的原生 Rerank HTTP 接口，并归一化两类响应格式。"""

    def __init__(self, settings: RerankSettings) -> None:
        self.settings = settings

    async def score(
        self, query: str, documents: list[RerankDocument]
    ) -> list[float]:
        headers = {
            "Authorization": (
                f"Bearer {self.settings.api_key.get_secret_value()}"
            )
        }
        payload = {
            "model": self.settings.model,
            "query": query,
            "documents": [item.text for item in documents],
            "top_n": len(documents),
        }
        async with httpx.AsyncClient(
            timeout=self.settings.timeout_seconds
        ) as client:
            response = await client.post(
                self.settings.base_url.rstrip("/"),
                headers=headers,
                json=payload,
            )
            response.raise_for_status()
            body = response.json()
        if isinstance(body.get("scores"), list):
            return [float(score) for score in body["scores"]]
        results = body.get("results")
        if isinstance(results, list):
            scores = [0.0] * len(documents)
            for item in results:
                scores[int(item["index"])] = float(
                    item.get("relevance_score", item.get("score"))
                )
            return scores
        raise ModelCallError(
            "invalid_rerank_response",
            "Rerank 响应缺少 scores 或 results",
            retryable=False,
        )


class RerankModel:
    def __init__(
        self,
        settings: RerankSettings,
        backend: RerankBackend | None = None,
    ) -> None:
        self.settings = settings
        self.backend = backend or HttpRerankBackend(settings)

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
                return [
                    RerankResult(document_id=document.document_id, score=None)
                    for document in documents[:limit]
                ]
            if isinstance(exc, ModelCallError):
                raise
            raise ModelCallError(
                "rerank_failed", str(exc), retryable=True
            ) from exc
