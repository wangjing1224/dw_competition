"""保留 Agent 会话的模型/工具诊断，但不把图片二进制上传到 Trace。"""

from langchain_core.messages import BaseMessage
from langsmith import traceable, tracing_context


def trace_payload(value):
    """只脱敏追踪副本；真正送给模型的多模态消息保持不变。"""
    if isinstance(value, BaseMessage):
        value = value.model_dump(mode="json")
    if isinstance(value, dict):
        return {key: trace_payload(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [trace_payload(item) for item in value]
    if isinstance(value, str) and value.startswith("data:image/"):
        return "[图片二进制已省略；位置见同条工具消息]"
    return value


@traceable(
    name="agent_model",
    run_type="llm",
    process_inputs=lambda values: trace_payload(
        {
            "messages": values["messages"],
            "tools": values["tool_schemas"],
        }
    ),
    process_outputs=trace_payload,
)
async def _model_call(runnable, messages, tool_schemas, call_options):
    # 禁用底层 LangChain 再次自动采集未经脱敏的同一条消息。
    with tracing_context(enabled=False):
        return await runnable.ainvoke(messages, **call_options)


async def invoke_agent_model(
    runnable, messages, tools, context, *, trace_name="agent_model", **call_options
):
    with tracing_context(**context):
        return await _model_call(
            runnable,
            messages,
            [
                {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.tool_call_schema.model_json_schema(),
                }
                for tool in tools
            ],
            call_options,
            langsmith_extra={"name": trace_name},
        )


@traceable(
    name="agent_tool",
    run_type="tool",
    process_inputs=lambda values: trace_payload(values["call"]),
    process_outputs=trace_payload,
)
async def _tool_call(tool, call):
    with tracing_context(enabled=False):
        return await tool.ainvoke(call)


async def invoke_agent_tool(tool, call, context):
    with tracing_context(**context):
        return await _tool_call(tool, call, langsmith_extra={"name": call["name"]})
