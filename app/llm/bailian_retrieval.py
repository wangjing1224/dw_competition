"""百炼原生文本向量/重排：批次映射、共享限流和有限重试。

协议依据：https://help.aliyun.com/zh/model-studio/text-embedding-synchronous-api
以及 https://help.aliyun.com/zh/model-studio/text-rerank-api （2026-09-27）。
不导入本地权重库；构造适配器不联网。HTTP 客户端在首次请求时创建。
"""

from __future__ import annotations

import asyncio
import logging
import math
import random
import re
import time
import weakref
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import TYPE_CHECKING

import httpx

from app.llm.errors import ModelCallError

if TYPE_CHECKING:
    from app.core.config import EmbeddingSettings, RerankSettings
    from app.llm.rerank_model import RerankDocument

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class EmbeddingLimits:
    max_items: int
    max_tokens: int
    dimensions: tuple[int, ...]


EMBEDDING_MODELS = {
    "qwen3.7-text-embedding": EmbeddingLimits(
        20, 128_000, (256, 512, 768, 1024, 1536, 2048, 2560)
    ),
    "qwen3.7-text-embedding-flash": EmbeddingLimits(20, 128_000, (256, 512, 768, 1024)),
    "text-embedding-v4": EmbeddingLimits(
        10, 8192, (64, 128, 256, 512, 768, 1024, 1536, 2048)
    ),
}
RERANK_MAX_ITEMS = 500
RERANK_MAX_TOKENS = 30_000
# 官方建议值用于客户端批次规划，不当作服务端必定拒绝的硬阈值。
RERANK_BATCH_TOKEN_TARGET = 120_000
EMBEDDING_PATH = "/services/embeddings/text-embedding/text-embedding"
RERANK_PATH = "/services/rerank/text-rerank/text-rerank"


@dataclass
class _Pool:
    limit: int
    semaphore: asyncio.Semaphore
    client: httpx.AsyncClient | None = None


# 同一事件循环、服务地址、职责及型号共享名额，避免多个Agent各建一个限流器。
# Key不参与键值，也不保存在共享客户端默认头中。
_pools: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()


async def close_retrieval_clients() -> None:
    """FastAPI退出时调用；独立 asyncio.run 脚本也应在 finally 调用。"""
    pools = _pools.pop(asyncio.get_running_loop(), {})
    await asyncio.gather(
        *(pool.client.aclose() for pool in pools.values() if pool.client is not None)
    )


def _invalid(message: str) -> ModelCallError:
    return ModelCallError("invalid_retrieval_response", message, retryable=False)


def _ordered_items(data: dict, field: str, index_key: str, count: int) -> list[dict]:
    output = data.get("output")
    items = output.get(field) if isinstance(output, dict) else None
    if not isinstance(items, list) or len(items) != count:
        raise _invalid(f"{field} 数量不等于输入数量 {count}")
    ordered: dict[int, dict] = {}
    for item in items:
        index = item.get(index_key) if isinstance(item, dict) else None
        if type(index) is not int or not 0 <= index < count or index in ordered:
            raise _invalid(f"{field} 包含非法、重复或越界索引")
        ordered[index] = item
    return [ordered[index] for index in range(count)]


def _numeric(value) -> bool:
    try:
        return type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        return False


def _safe_identifier(value) -> str:
    # 厂商错误文本可能回显原文；日志仅保留短标识符，不记录响应body/message。
    return re.sub(r"[^a-zA-Z0-9_.:-]", "_", str(value or "unknown"))[:120]


def _retry_delay(header: str | None, attempt: int) -> float:
    if header:
        try:
            delay = float(header)
        except ValueError:
            try:
                date = parsedate_to_datetime(header)
                delay = (date - datetime.now(UTC)).total_seconds()
            except (ValueError, TypeError, OverflowError):
                delay = -1
        if math.isfinite(delay) and delay >= 0:
            return delay
    return min(2**attempt, 8) + random.uniform(0, 0.25)


class BailianHTTP:
    """只负责原生协议请求；注入client用于HTTP Mock，生命周期归注入方。"""

    def __init__(
        self, settings, service: str, *, client: httpx.AsyncClient | None = None
    ):
        self.settings = settings
        self.service = service
        self.base_url = settings.base_url.rstrip("/")
        self._client = client
        url = httpx.URL(self.base_url)
        if (
            url.scheme not in {"https", "http"}
            or not url.host
            or url.path != "/api/v1"
            or url.query
            or url.fragment
            or url.userinfo
        ):
            raise ValueError(
                f"{service} 百炼 base_url 应为控制台API Host加 /api/v1，不是兼容/Chat端点"
            )
        if not settings.api_key.get_secret_value().strip():
            raise ValueError(f"{service} 未配置百炼 API Key")

    def _pool(self) -> _Pool:
        pools = _pools.setdefault(asyncio.get_running_loop(), {})
        key = (self.base_url, self.service, self.settings.model)
        pool = pools.get(key)
        if pool is None:
            pool = pools[key] = _Pool(
                self.settings.max_concurrency,
                asyncio.Semaphore(self.settings.max_concurrency),
            )
        elif pool.limit != self.settings.max_concurrency:
            raise ValueError("同一检索服务的并发配置不一致，请统一配置后重启")
        return pool

    async def post(self, path: str, payload: dict, *, count: int) -> dict:
        pool = self._pool()
        if self._client is None and pool.client is None:
            pool.client = httpx.AsyncClient(follow_redirects=False)
        client = self._client if self._client is not None else pool.client
        for attempt in range(self.settings.max_retries + 1):
            started = time.monotonic()
            response = None
            try:
                async with pool.semaphore:
                    response = await client.post(
                        self.base_url + path,
                        headers={
                            "Authorization": "Bearer "
                            + self.settings.api_key.get_secret_value()
                        },
                        json=payload,
                        timeout=self.settings.timeout_seconds,
                    )
                try:
                    data = response.json()
                except ValueError:
                    data = {}
                if not isinstance(data, dict):
                    data = {}
                status = response.status_code
                error = data.get("error")
                provider_code = (
                    error.get("code") if isinstance(error, dict) else None
                ) or data.get("code")
                failed = status != 200 or provider_code is not None or error is not None
                request_id = _safe_identifier(
                    data.get("request_id") or response.headers.get("x-request-id")
                )
                logger.info(
                    "bailian %s model=%s items=%d status=%d attempt=%d seconds=%.3f request_id=%s",
                    self.service,
                    self.settings.model,
                    count,
                    status,
                    attempt + 1,
                    time.monotonic() - started,
                    request_id,
                )
                if not failed:
                    usage = data.get("usage")
                    if isinstance(usage, dict):
                        logger.info(
                            "bailian %s tokens=%s",
                            self.service,
                            {
                                k: v
                                for k, v in usage.items()
                                if k in {"total_tokens", "prompt_tokens"}
                                and type(v) is int
                            },
                        )
                    return data
                retryable = status in {408, 429, 500, 502, 503, 504}
                error = ModelCallError(
                    f"{self.service}_api_error",
                    f"百炼{self.service}失败：HTTP {status}，code={_safe_identifier(provider_code)}，"
                    f"request_id={request_id}；请检查权限、额度及该模型输入限制。",
                    retryable=retryable,
                )
            except httpx.TransportError as exc:
                error = ModelCallError(
                    f"{self.service}_connection_error",
                    f"百炼{self.service}连接失败：{type(exc).__name__}",
                    retryable=True,
                )
            if not error.retryable or attempt >= self.settings.max_retries:
                raise error
            # 不在退避等待期间占用请求名额；取消任务时不拦截 CancelledError。
            await asyncio.sleep(
                _retry_delay(
                    response.headers.get("retry-after")
                    if response is not None
                    else None,
                    attempt,
                )
            )
        raise AssertionError("unreachable")


class BailianEmbeddings:
    def __init__(
        self, settings: EmbeddingSettings, *, client: httpx.AsyncClient | None = None
    ):
        self.settings = settings
        self.http = BailianHTTP(settings, "embedding", client=client)

    async def _embed(self, texts: list[str], text_type: str) -> list[list[float]]:
        if not texts:
            return []
        if len(texts) > EMBEDDING_MODELS[self.settings.model].max_items:
            raise ModelCallError(
                "embedding_batch_too_large",
                "Embedding批量超过型号上限",
                retryable=False,
            )
        if any(not isinstance(text, str) or not text.strip() for text in texts):
            raise ModelCallError(
                "embedding_empty_text", "Embedding输入不能含空文本", retryable=False
            )
        # 没有精确Qwen tokenizer时，不用字数/UTF8估计硬拒绝长表格；服务端判定单条超限。
        data = await self.http.post(
            EMBEDDING_PATH,
            {
                "model": self.settings.model,
                "input": {"texts": texts},
                "parameters": {
                    "dimension": self.settings.dimension,
                    "text_type": text_type,
                    "output_type": "dense",
                },
            },
            count=len(texts),
        )
        vectors = []
        for item in _ordered_items(data, "embeddings", "text_index", len(texts)):
            vector = item.get("embedding")
            if (
                not isinstance(vector, list)
                or len(vector) != self.settings.dimension
                or any(not _numeric(value) for value in vector)
            ):
                raise _invalid("Embedding向量维度或数值不合法")
            vectors.append(vector)
        return vectors

    async def aembed_documents(self, texts: list[str]) -> list[list[float]]:
        return await self._embed(texts, "document")

    async def aembed_query(self, text: str) -> list[float]:
        return (await self._embed([text], "query"))[0]


class BailianRerankBackend:
    def __init__(
        self, settings: RerankSettings, *, client: httpx.AsyncClient | None = None
    ):
        self.settings = settings
        self.http = BailianHTTP(settings, "rerank", client=client)

    async def score(self, query: str, documents: list[RerankDocument]) -> list[float]:
        if not documents:
            return []
        if not query.strip() or any(not doc.text.strip() for doc in documents):
            raise ModelCallError(
                "rerank_empty_text", "重排查询和候选不能为空", retryable=False
            )
        # UTF8只用作保守批次估算。单条超过估算预算仍单独提交，不能冒充精确Token判定。
        overhead = len((query + self.settings.instruction).encode("utf-8"))
        batches: list[list[RerankDocument]] = []
        batch = []
        used = 0
        for document in documents:
            cost = overhead + len(document.text.encode("utf-8"))
            if batch and (
                len(batch) >= RERANK_MAX_ITEMS
                or used + cost > RERANK_BATCH_TOKEN_TARGET
            ):
                batches.append(batch)
                batch, used = [], 0
            batch.append(document)
            used += cost
        if batch:
            batches.append(batch)
        scores = []
        for batch in batches:
            try:
                data = await self.http.post(
                    RERANK_PATH,
                    {
                        "model": self.settings.model,
                        "input": {
                            "query": query,
                            "documents": [doc.text for doc in batch],
                        },
                        "parameters": {
                            "top_n": len(batch),
                            "instruct": self.settings.instruction,
                        },
                    },
                    count=len(batch),
                )
                for item in _ordered_items(data, "results", "index", len(batch)):
                    score = item.get("relevance_score")
                    if not _numeric(score):
                        raise _invalid("Rerank返回了非有限或非数值分数")
                    scores.append(float(score))
            except ModelCallError as exc:
                raise ModelCallError(
                    exc.code,
                    f"{exc.message}；候选ID：{','.join(doc.document_id for doc in batch)}",
                    retryable=exc.retryable,
                ) from exc
        return scores
