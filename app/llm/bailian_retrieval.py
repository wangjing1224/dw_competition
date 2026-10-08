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
from html import escape
from html.parser import HTMLParser
from typing import TYPE_CHECKING

import httpx
from lxml import etree

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
# 官方上限与客户端安全目标分开。下面规划使用UTF-8字节作保守代理，非精确Token。
RERANK_DOCUMENT_TARGET = 25_000
RERANK_QUERY_TARGET = 4_000
# 官方建议值用于客户端批次规划，不当作服务端必定拒绝的硬阈值。
RERANK_BATCH_TOKEN_TARGET = 120_000
EMBEDDING_PATH = "/services/embeddings/text-embedding/text-embedding"
RERANK_PATH = "/services/rerank/text-rerank/text-rerank"


def _byte_size(text: str) -> int:
    return len(text.encode("utf-8"))


def _planning_error(message: str) -> ModelCallError:
    return ModelCallError("rerank_input_budget", message, retryable=False)


class _ScoringHTML(HTMLParser):
    """仅改打分副本中td/th的无效合并属性；数据、其他标签和真正合并原样保留。"""

    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        def redundant(name, value):
            return (
                tag in {"td", "th"}
                and name in {"rowspan", "colspan"}
                and value is not None
                and re.fullmatch(r"\s*0*1\s*", value) is not None
            )

        if any(redundant(name, value) for name, value in attrs):
            kept = "".join(
                f' {name}="{escape(value, quote=True)}"' if value is not None else f" {name}"
                for name, value in attrs if not redundant(name, value)
            )
            self.parts.append(f"<{tag}{kept}>")
        else:
            self.parts.append(self.get_starttag_text())

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if not self.parts[-1].endswith("/>"):
            self.parts[-1] = self.parts[-1][:-1] + "/>"

    def handle_endtag(self, tag):
        self.parts.append(f"</{tag}>")

    def handle_data(self, data):
        self.parts.append(data)

    def handle_entityref(self, name):
        self.parts.append(f"&{name};")

    def handle_charref(self, name):
        self.parts.append(f"&#{name};")

    def handle_comment(self, data):
        self.parts.append(f"<!--{data}-->")

    def handle_decl(self, decl):
        self.parts.append(f"<!{decl}>")

    def handle_pi(self, data):
        self.parts.append(f"<?{data}>")


def _clean_scoring_text(text: str) -> str:
    if not re.search(r"<table\b", text, re.I):
        return text
    parser = _ScoringHTML()
    parser.feed(text)
    parser.close()
    return "".join(parser.parts)


def _text_parts(text: str, budget: int) -> list[str]:
    """连续文本窗口，优先段落/语句边界；无边界长单元格也保留尾部。"""
    if budget < 16:
        raise _planning_error("必要表头/对象/合并上下文无法装入打分窗口（UTF-8字节代理）")
    remaining = text
    parts = []
    while _byte_size(remaining) > budget:
        prefix = remaining.encode("utf-8")[:budget].decode("utf-8", errors="ignore")
        boundaries = list(re.finditer(r"\n|[。；;!?！？]\s*|\s+", prefix))
        cut = boundaries[-1].end() if boundaries else len(prefix)
        if cut < len(prefix) // 2:
            cut = len(prefix)
        parts.append(remaining[:cut])
        remaining = remaining[cut:]
    if remaining:
        parts.append(remaining)
    return parts


def _table_units(table, context: str, target: int):
    """正常行连续分窗；大行按列细分，嵌套表递归，跨行合并文本随对应行携带。"""
    rows = table.xpath("./tr | ./thead/tr | ./tbody/tr | ./tfoot/tr")
    caption = " ".join(table.xpath("./caption//text()"))
    base = "\n".join(part for part in (context, caption) if part)
    headers = [
        row for row in rows
        if row.getparent().tag == "thead" or (row.xpath("./th") and not row.xpath("./td"))
    ]
    # td形式首行不冒称表头，但在足够短时作为上下文重复，保留常见列名/单位。
    if not headers and rows and _byte_size(" ".join(rows[0].itertext())) <= 1024:
        headers = [rows[0]]
    header_text = "\n".join(etree.tostring(row, encoding="unicode", with_tail=False) for row in headers)
    base = "\n".join(part for part in (base, header_text) if part)
    active: list[tuple[int, int, int, str]] = []
    for number, row in enumerate(rows, 1):
        active = [span for span in active if span[0] >= number]
        carried = "\n".join(f"跨行列{col + 1}：{value}" for _, col, _, value in active)
        row_context = "\n".join(part for part in (base, carried) if part)
        cells, column = [], 0
        for cell in row.xpath("./td | ./th"):
            while any(c <= column < c + width for _, c, width, _ in active):
                column += 1
            try:
                rs, cs = int(cell.get("rowspan", 1)), int(cell.get("colspan", 1))
            except ValueError as exc:
                raise _planning_error("表格跨度无效，无法安全建立打分窗口") from exc
            if rs < 1 or cs < 1 or rs > len(rows) - number + 1 or cs > 10000:
                raise _planning_error("表格跨度越界，无法安全建立打分窗口")
            value = etree.tostring(cell, encoding="unicode", with_tail=False)
            cells.append((column, cell, value))
            if rs > 1:
                active.append((number + rs - 1, column, cs, value))
            column += cs
        row_text = f"行{number}\n" + etree.tostring(row, encoding="unicode", with_tail=False)
        if _byte_size(row_context + row_text) <= target - 64:
            yield row_context, row_text
            continue
        # 大行分列仍携带短对象列；大单元格不被当成所有后续列的必带上下文。
        anchors = "\n".join(
            f"对象列{col + 1}：{value}" for col, _, value in cells[:2]
            if _byte_size(value) <= 512
        )
        for col, cell, _ in cells:
            cell_context = "\n".join((
                row_context,
                f"行{number} / 列{col + 1} (rowspan={cell.get('rowspan', '1')}, colspan={cell.get('colspan', '1')})",
                anchors,
            ))
            yield from _element_units(cell, cell_context, target)


def _element_units(element, context: str, target: int):
    if element.tag == "table":
        yield from _table_units(element, context, target)
        return
    if element.text:
        yield context, element.text
    for child in element:
        if isinstance(child.tag, str):
            yield from _element_units(child, context, target)
        if child.tail:
            yield context, child.tail


def _scoring_windows(text: str, target: int) -> list[str]:
    if _byte_size(text) <= target:
        return [text]
    if re.search(r"<table\b", text, re.I):
        parser = etree.HTMLParser(no_network=True)
        root = etree.HTML(text, parser=parser)
        if root is None or any(error.level >= 3 for error in parser.error_log):
            raise _planning_error("表格结构无法完整读取，未截尾提交打分")
        # 由检索层附在正文之前的简短定位提示随窗口保留，不猜业务对象。
        prefix = text[:re.search(r"<table\b", text, re.I).start()].strip()
        context = prefix if _byte_size(prefix) <= 2048 else ""
        units = _element_units(root, context, target)
    else:
        units = [("", text)]
    windows, pending, last_context = [], "", None
    for context, body in units:
        if not body.strip() or (context and body.strip() == context.strip()):
            continue
        room = target - _byte_size(context) - 64
        for part in _text_parts(body, room):
            if context == last_context and _byte_size(context + pending + "\n" + part) <= target - 64:
                pending += "\n" + part
            else:
                if pending:
                    windows.append((last_context + "\n" + pending).strip())
                pending, last_context = part, context
    if pending:
        windows.append((last_context + "\n" + pending).strip())
    if not windows:
        raise _planning_error("打分副本无可用正文")
    return [f"窗口 {i}/{len(windows)}\n{value}" for i, value in enumerate(windows, 1)]


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


def _safe_length_diagnostic(data: dict) -> str:
    """仅输出白名单分类/数值；厂商message可能回显业务正文，绝不原样记录。"""
    error = data.get("error")
    detail = error if isinstance(error, dict) else data
    message = str(detail.get("message", "")).lower()
    kind = "unknown"
    if any(word in message for word in ("exceed", "too long", "maximum", "length limit")):
        if "document" in message and "token" in message:
            kind = "document_length"
        elif "query" in message and "token" in message:
            kind = "query_length"
        elif any(word in message for word in ("total token", "request token", "batch")):
            kind = "request_length"
    metrics = {
        key: detail[key] for key in ("max_tokens", "input_tokens", "document_index", "max_documents")
        if type(detail.get(key)) is int and 0 <= detail[key] <= 1_000_000_000
    }
    return f"constraint={kind}, length_metrics={metrics}"


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
                    f"request_id={request_id}，{_safe_length_diagnostic(data)}；请检查权限、额度及该模型输入限制。",
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

    async def _rank_batch(self, query, documents, limit, *, phase):
        overhead = _byte_size(query + self.settings.instruction)
        lengths = [_byte_size(doc.text) for doc in documents]
        if (
            len(documents) > RERANK_MAX_ITEMS
            or sum(lengths) + overhead * len(documents) > RERANK_BATCH_TOKEN_TARGET
            or any(length + overhead > RERANK_DOCUMENT_TARGET for length in lengths)
        ):
            raise _planning_error("打分批次超出客户端字节代理预算，未发送请求")
        logger.info(
            "bailian rerank plan phase=%s model=%s method=utf8_bytes_proxy "
            "items=%d top_n=%d chars=%s bytes=%s overhead_bytes=%d ids=%s",
            phase, self.settings.model, len(documents), limit,
            [len(doc.text) for doc in documents], lengths, overhead,
            [_safe_identifier(doc.document_id) for doc in documents],
        )
        try:
            data = await self.http.post(
                RERANK_PATH,
                {
                    "model": self.settings.model,
                    "input": {"query": query, "documents": [doc.text for doc in documents]},
                    "parameters": {"top_n": limit, "instruct": self.settings.instruction},
                },
                count=len(documents),
            )
            output = data.get("output")
            items = output.get("results") if isinstance(output, dict) else None
            if not isinstance(items, list) or len(items) != limit:
                raise _invalid("Rerank结果数量与本批top_n不一致")
            seen, results = set(), []
            for item in items:
                index = item.get("index") if isinstance(item, dict) else None
                if type(index) is not int or not 0 <= index < len(documents) or index in seen:
                    raise _invalid("Rerank包含非法、重复或越界索引")
                score = item.get("relevance_score")
                if not _numeric(score):
                    raise _invalid("Rerank返回了非有限或非数值分数")
                seen.add(index)
                results.append((documents[index], float(score)))
            # 只对同一次返回排序，绝不跨请求比较raw score。
            return sorted(results, key=lambda item: item[1], reverse=True)
        except ModelCallError as exc:
            raise ModelCallError(
                exc.code,
                f"{exc.message}；候选ID：{','.join(_safe_identifier(doc.document_id) for doc in documents)}",
                retryable=exc.retryable,
            ) from exc

    async def _rolling_top_k(self, query, documents, k, *, phase):
        overhead = _byte_size(query + self.settings.instruction)
        pool, cursor = [], 0
        while cursor < len(documents):
            batch = list(pool)
            used = sum(_byte_size(doc.text) + overhead for doc in batch)
            end = cursor
            # 按原候选顺序尽量装满；新候选不跳过、不仅取头中尾。
            while end < len(documents) and len(batch) < RERANK_MAX_ITEMS:
                cost = _byte_size(documents[end].text) + overhead
                if used + cost > RERANK_BATCH_TOKEN_TARGET:
                    break
                batch.append(documents[end])
                used += cost
                end += 1
            final = end == len(documents)
            if end == cursor or (not final and len(batch) <= k):
                raise _planning_error("必要上下文无法容纳K+1候选，未返回局部Top-K")
            ranked = await self._rank_batch(query, batch, min(k, len(batch)), phase=phase)
            if final:
                return ranked
            pool = [doc for doc, _ in ranked]
            cursor = end
        return []

    async def select_top_k(self, query, documents, top_n):
        """百炼内部路径：窗口先选代表，再滚动筛选；只返回最终同批次的分数。"""
        if not documents:
            return []
        if not query.strip() or any(not doc.text.strip() for doc in documents):
            raise ModelCallError("rerank_empty_text", "重排查询和候选不能为空", retryable=False)
        if _byte_size(query) > RERANK_QUERY_TARGET:
            raise _planning_error("查询超出客户端4000 UTF-8字节保守代理预算；未截断查询")
        if len({doc.document_id for doc in documents}) != len(documents):
            raise _planning_error("候选ID重复，无法可靠回链")
        if type(top_n) is not int or top_n <= 0:
            raise _planning_error("top_n必须为正整数")
        k = min(top_n, len(documents))
        required = min(k + 1, len(documents))
        if required > RERANK_MAX_ITEMS:
            raise _planning_error("所需Top-K无法在服务商最大候选数内共同比较")
        overhead = _byte_size(query + self.settings.instruction)
        cleaned = [doc.model_copy(update={"text": _clean_scoring_text(doc.text)}) for doc in documents]
        # 在第一次比较前规划所有表示；避免修改表示后漏掉已淘汰候选。
        target = RERANK_DOCUMENT_TARGET - overhead
        costs = sorted((min(_byte_size(doc.text), target) + overhead for doc in cleaned), reverse=True)
        if sum(costs[:required]) > RERANK_BATCH_TOKEN_TARGET:
            target = min(target, RERANK_BATCH_TOKEN_TARGET // required - overhead)
        # 一个原候选多个窗口的代表选择也须至少容纳两个窗口（K=1）。
        if any(_byte_size(doc.text) > target for doc in cleaned):
            target = min(target, RERANK_BATCH_TOKEN_TARGET // 2 - overhead)
        if target < 80:
            raise _planning_error("查询/instruct及必要上下文无法容纳候选比较，未降低K或丢候选")
        prepared = [(doc, _scoring_windows(doc.text, target)) for doc in cleaned]
        representatives = []
        for doc, windows in prepared:
            logger.info(
                "bailian rerank windows id=%s windows=%d target_bytes=%d cleaned_chars=%d cleaned_bytes=%d",
                _safe_identifier(doc.document_id), len(windows), target, len(doc.text), _byte_size(doc.text),
            )
            if len(windows) == 1:
                representative = windows[0]
            else:
                candidates = [
                    doc.model_copy(update={"document_id": f"{doc.document_id}::window-{i}", "text": text})
                    for i, text in enumerate(windows, 1)
                ]
                representative = (await self._rolling_top_k(query, candidates, 1, phase="window"))[0][0].text
            representatives.append(doc.model_copy(update={"text": representative}))
        ranked = await self._rolling_top_k(query, representatives, k, phase="candidate")
        return [(doc.document_id, score) for doc, score in ranked]

    async def score(self, query: str, documents: list[RerankDocument]) -> list[float]:
        """兼容全量评分调用，但不拼接跨批次的不可比分数；业务入口使用select_top_k。"""
        ranked = await self.select_top_k(query, documents, len(documents))
        scores = dict(ranked)
        return [scores[doc.document_id] for doc in documents]
