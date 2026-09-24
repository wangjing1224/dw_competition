"""只读请求核算，以及可选的完整结构分批；不访问任何业务模型或数据库。"""

import json
from copy import deepcopy
from dataclasses import dataclass, field
from hashlib import sha256
from typing import Literal

from langchain_core.messages import BaseMessage

from app.llm.platforms import ChatProtocol
from app.llm.token_counter import CounterUnavailable, TokenCounter


def canonical(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value) -> str:
    return sha256(canonical(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class InputContext:
    """保持唯一的有序消息。bindings 是实际传给 Chat 接口的工具/输出约束。"""

    messages: tuple[BaseMessage, ...]
    bindings: dict = field(default_factory=dict)
    protocol: ChatProtocol = field(default_factory=ChatProtocol)

    def __post_init__(self):
        object.__setattr__(self, "messages", tuple(deepcopy(self.messages)))
        object.__setattr__(self, "bindings", deepcopy(self.bindings))
        if set(self.bindings) - {
            "tools",
            "tool_choice",
            "response_format",
            "parallel_tool_calls",
        }:
            raise ValueError("bindings 只放工具及输出约束，不放 Key 或生成参数")

    def wire_messages(self) -> list[dict]:
        return self.protocol.wire_messages(
            self.messages, has_tools=bool(self.bindings.get("tools"))
        )


def validate_tool_pairs(messages: list[dict]) -> None:
    """工具组必须完整闭合；不能把提醒插在并列工具的两个结果之间。"""
    pending = set()
    for message in messages:
        if message["role"] == "tool":
            call_id = message.get("tool_call_id")
            if call_id not in pending:
                raise ValueError("孤立或重复的 ToolMessage")
            pending.remove(call_id)
        else:
            if pending:
                raise ValueError("工具调用未闭合，不能发送下一条非工具消息")
            calls = message.get("tool_calls", [])
            ids = [call.get("id") for call in calls]
            if any(not value for value in ids) or len(ids) != len(set(ids)):
                raise ValueError("工具调用 ID 缺失或重复")
            pending.update(ids)
            if message.get("function_call"):
                raise ValueError("当前控制器不支持旧 function_call 协议")
    if pending:
        raise ValueError("工具结果尚未全部返回")


@dataclass(frozen=True)
class BudgetSettings:
    context_window_tokens: int
    compaction_trigger_ratio: float = 0.90
    model_key: str = ""

    def __post_init__(self):
        if self.context_window_tokens <= 0 or not 0 < self.compaction_trigger_ratio < 1:
            raise ValueError("上下文容量与触发比例配置无效")

    @property
    def threshold(self):
        return int(self.context_window_tokens * self.compaction_trigger_ratio)


@dataclass(frozen=True)
class InputEstimate:
    # 四类分项为本地诊断估算；usage 校准总量不冒充服务端四类明细。
    parts: dict[str, int]
    total: int
    threshold: int
    capacity: int
    method: str
    local_total: int

    @property
    def remaining(self):
        return self.threshold - self.total


@dataclass(frozen=True)
class BudgetDecision:
    status: Literal["ready", "over_budget", "blocked"]
    estimate: InputEstimate | None
    reason: str
    request_key: str = ""
    conditions_key: str = ""
    baseline_key: str = ""


@dataclass(frozen=True)
class UsageBaseline:
    """仅最近一次成功请求；不包含其回复或其他 Agent 的用量。"""

    message_hashes: tuple[str, ...]
    bindings_hash: str
    conditions_key: str
    input_tokens: int
    parts: dict[str, int]

    @property
    def key(self):
        return digest(
            (
                self.message_hashes,
                self.bindings_hash,
                self.conditions_key,
                self.input_tokens,
            )
        )


def _count_message(message: dict, counter: TokenCounter) -> int:
    value = deepcopy(message)
    visual = 0
    if isinstance(value.get("content"), list):
        content = []
        for block in value["content"]:
            if isinstance(block, dict) and block.get("type") == "image_url":
                visual += counter.count_image()
                # 真实图像按视觉预算计；URL/Base64 不进入文本分词器。
                content.append({"type": "image_url"})
            elif isinstance(block, dict) and block.get("type") != "text":
                raise CounterUnavailable("尚无此多模态内容类型的计数策略")
            else:
                content.append(block)
        value["content"] = content
    if value.get("audio"):
        raise CounterUnavailable("尚无音频计数策略")
    # JSON 编码与消息封装为偏保守近似，服务端模板仍可能不同。
    return counter.count_text(canonical(value)) + visual + 8


def check_input(
    *,
    context: InputContext,
    budget_settings: BudgetSettings,
    counter: TokenCounter,
    usage_baseline: UsageBaseline | None = None,
    previous: BudgetDecision | None = None,
) -> BudgetDecision:
    """计算不改变消息，不选择组织策略；相同请求与条件可复用已算结果。"""
    conditions = digest(
        (
            budget_settings.context_window_tokens,
            budget_settings.compaction_trigger_ratio,
            budget_settings.model_key,
            counter.signature,
            (
                context.protocol.platform,
                context.protocol.model,
                context.protocol.thinking,
            ),
        )
    )
    messages = context.wire_messages()
    hashes = tuple(digest(message) for message in messages)
    bindings_hash = digest(context.bindings)
    request_key = digest((hashes, bindings_hash))
    baseline_key = usage_baseline.key if usage_baseline else ""
    if previous and (
        previous.request_key,
        previous.conditions_key,
        previous.baseline_key,
    ) == (request_key, conditions, baseline_key):
        return previous
    try:
        validate_tool_pairs(messages)
        base = usage_baseline
        incremental = bool(
            base
            and base.conditions_key == conditions
            and base.bindings_hash == bindings_hash
            and hashes[: len(base.message_hashes)] == base.message_hashes
        )
        if incremental:
            parts = dict(base.parts)
            start = len(base.message_hashes)
        else:
            parts = {"system": 0, "bindings": 0, "human": 0, "interaction": 0}
            parts["bindings"] = (
                counter.count_text(canonical(context.bindings)) + 8
                if context.bindings
                else 0
            )
            start = 0
        added = 0
        for message in messages[start:]:
            role = message["role"]
            if role not in {"system", "developer", "user", "assistant", "tool"}:
                raise CounterUnavailable("不支持的消息角色")
            group = {"system": "system", "developer": "system", "user": "human"}.get(
                role, "interaction"
            )
            amount = _count_message(message, counter)
            parts[group] += amount
            added += amount
        local_total = sum(parts.values()) + 3  # assistant 起始封装只加一次
        total = base.input_tokens + added if incremental else local_total
        estimate = InputEstimate(
            parts,
            total,
            budget_settings.threshold,
            budget_settings.context_window_tokens,
            ("usage+" if incremental else "local:") + counter.name,
            local_total,
        )
        ready = total < estimate.threshold
        return BudgetDecision(
            "ready" if ready else "over_budget",
            estimate,
            "预算通过" if ready else "输入达到压缩触发阈值",
            request_key,
            conditions,
            baseline_key,
        )
    except (CounterUnavailable, ValueError) as exc:
        return BudgetDecision(
            "blocked", None, str(exc), request_key, conditions, baseline_key
        )


def usage_baseline_from_response(context, decision, response, *, confirmed=False):
    if not confirmed or decision.estimate is None:
        return None
    usage = getattr(response, "usage_metadata", None) or {}
    tokens = usage.get("input_tokens")
    if tokens is None:
        usage = getattr(response, "response_metadata", {}).get("token_usage", {}) or {}
        tokens = usage.get("prompt_tokens")
    if not isinstance(tokens, int) or isinstance(tokens, bool) or tokens <= 0:
        return None
    return UsageBaseline(
        tuple(digest(message) for message in context.wire_messages()),
        digest(context.bindings),
        decision.conditions_key,
        tokens,
        dict(decision.estimate.parts),
    )


@dataclass(frozen=True)
class InputBatch:
    status: Literal["ready", "done", "blocked"]
    start: int
    end: int
    context: InputContext | None = None
    decision: BudgetDecision | None = None
    blocked_reason: str | None = None


def plan_document_input(*, units, cursor, build_context, budget_settings, counter):
    """选取连续完整结构。只选择范围，不修改业务游标；没有此需求可不调用。"""
    if not units:
        return InputBatch("blocked", cursor, cursor, blocked_reason="没有可阅读结构")
    if not 0 <= cursor <= len(units):
        raise ValueError("阅读游标越界")
    if cursor == len(units):
        return InputBatch("done", cursor, cursor)

    def candidate(end):
        context = build_context(cursor, end)
        decision = check_input(
            context=context, budget_settings=budget_settings, counter=counter
        )
        return InputBatch("ready", cursor, end, context, decision)

    whole = candidate(len(units))
    if whole.decision.status == "ready":
        return whole
    if whole.decision.status == "blocked":
        return InputBatch(
            "blocked", cursor, cursor, blocked_reason=whole.decision.reason
        )
    # 只缩小连续前缀，不跳过超大结构去挑后面的短段。每个候选都按最终模板计数。
    low, high, best = cursor + 1, len(units) - 1, None
    while low <= high:
        end = (low + high) // 2
        current = candidate(end)
        if current.decision.status == "blocked":
            return InputBatch(
                "blocked", cursor, cursor, blocked_reason=current.decision.reason
            )
        if current.decision.status == "ready":
            best, low = current, end + 1
        else:
            high = end - 1
    if best:
        return best
    return InputBatch(
        "blocked",
        cursor,
        cursor,
        blocked_reason=f"位置 {cursor} 的完整结构与固定输入无法同时放入预算，未截断或跳过",
    )
