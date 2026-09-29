"""规则侧保留原导入入口；模型/工具追踪统一复用去图片二进制的实现。"""

from app.llm.agent_tracing import (
    invoke_agent_model,
    trace_payload,
)
from app.llm.agent_tracing import (
    invoke_agent_tool as invoke_rule_tool,
)

__all__ = ["invoke_rule_model", "invoke_rule_tool", "trace_payload"]


async def invoke_rule_model(
    runnable, messages, tools, context, *, trace_name="rule_model", **call_options
):
    """保持规则 Agent 原有的 Trace 名称，实际消息处理共用。"""
    return await invoke_agent_model(
        runnable, messages, tools, context, trace_name=trace_name, **call_options
    )
