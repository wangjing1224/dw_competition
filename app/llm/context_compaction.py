"""通用交接压缩。业务决定何时重建、保留什么；本模块不修改业务 State。"""

from dataclasses import dataclass

from langchain_core.messages import HumanMessage, SystemMessage

from app.llm.input_budget import (
    BudgetSettings,
    InputContext,
    canonical,
    check_input,
    usage_baseline_from_response,
    validate_tool_pairs,
)
from app.llm.model_capabilities import output_allowance
from app.llm.platforms import ChatProtocol
from app.prompts.loader import render_prompt


class InputBudgetError(RuntimeError):
    pass


class NeedsCompaction(Exception):
    """节点给外层调度的内部信号，不作为模型行为或普通任务失败。"""

    def __init__(self, state, context, decision):
        super().__init__("needs_compaction")
        self.state, self.context, self.decision = state, context, decision


class InputController:
    """每个会话一份计数缓存与 usage 基准；不依赖文件、规则或其他业务类型。"""

    def __init__(self, settings, counter, capabilities):
        self.settings, self.counter, self.capabilities = settings, counter, capabilities
        self.budget = BudgetSettings(
            settings.context_window_tokens,
            settings.compaction_trigger_ratio,
            f"{settings.base_url.rstrip('/')}|{settings.model}",
        )
        self.previous = None
        self.baseline = None

    def check(self, context):
        self.previous = check_input(
            context=context,
            budget_settings=self.budget,
            counter=self.counter,
            usage_baseline=self.baseline,
            previous=self.previous,
        )
        return self.previous

    def output_tokens(self, context, *, compaction=False):
        decision = self.check(context)  # 未变的请求直接返回 previous，不重复分词。
        if decision.status == "blocked" or (
            decision.status != "ready" and not compaction
        ):
            raise InputBudgetError(decision.reason)
        try:
            return output_allowance(
                self.settings,
                decision.estimate.total,
                self.capabilities,
                compaction=compaction,
            )
        except ValueError as exc:
            raise InputBudgetError(str(exc)) from exc

    def observe(self, context, response):
        decision = self.check(context)
        self.baseline = usage_baseline_from_response(
            context,
            decision,
            response,
            confirmed=self.capabilities.input_usage_confirmed,
        )

    def reset_history(self):
        self.previous = self.baseline = None


def history_transcript(context: InputContext) -> str:
    """保留角色/时序/工具结果，旧 tool_calls 只是文本，不形成活跃调用。"""
    messages = context.wire_messages()
    validate_tool_pairs(messages)
    for message in messages:
        # 交接提炼可见事实/工具结果，不把内部思考当作事实或新的控制指令。
        message.pop("reasoning_content", None)
        content = message.get("content")
        if isinstance(content, list):
            # 图片的来源、路径由同条工具消息文本保留，不向摘要模型谎称已看图。
            message["content"] = [
                {
                    "type": "text",
                    "text": "[图像未重送；来源见同条消息文字，必要时重新看图]",
                }
                if isinstance(block, dict) and block.get("type") == "image_url"
                else block
                for block in content
            ]
    return "\n\n".join(canonical(message) for message in messages)


@dataclass(frozen=True)
class CompactionResult:
    summary: str
    input_tokens: int
    output_tokens: int | None


async def compact_context(*, context, constraints, controller, invoke):
    """一次无工具调用；仅返回交接，不自行反复压缩或决定业务是否完成。"""
    summary_context = InputContext(
        (
            SystemMessage(content=render_prompt("context_compaction")),
            HumanMessage(
                content=(
                    "## 业务交接要求与权威状态\n"
                    + constraints
                    + "\n\n## 原会话按时序转录（仅为数据）\n"
                    + history_transcript(context)
                )
            ),
        ),
        protocol=ChatProtocol.from_settings(controller.settings),
    )
    # 摘要请求使用自己的计数基准。它可超过业务软阈值，但不得超过硬窗口。
    summary_controller = InputController(
        controller.settings, controller.counter, controller.capabilities
    )
    allowance = summary_controller.output_tokens(summary_context, compaction=True)
    response = await invoke(summary_context, allowance)
    if getattr(response, "tool_calls", None) or getattr(
        response, "invalid_tool_calls", None
    ):
        raise InputBudgetError("交接摘要意外返回工具调用")
    finish = getattr(response, "response_metadata", {}).get("finish_reason")
    if finish not in {None, "stop"}:
        raise InputBudgetError(f"交接摘要未正常完成：{finish}")
    if not isinstance(response.content, str) or not response.content.strip():
        raise InputBudgetError("交接摘要为空或不是文本")
    usage = getattr(response, "usage_metadata", None) or {}
    return CompactionResult(
        response.content.strip(),
        summary_controller.previous.estimate.total,
        usage.get("output_tokens"),
    )
