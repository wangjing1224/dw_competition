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
    from app.audit.local_trace import scope

    with (
        scope(
            "model",
            "模型调用" if "compaction" not in trace_name else "上下文压缩模型",
            input={"role": trace_name},
        ) as observation,
        tracing_context(**context),
    ):
        response = await _model_call(
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
        # 仅可见正文和工具调用；不返回请求 messages/System/Human。
        observation["output"] = {
            "content": response.content,
            "tool_calls": getattr(response, "tool_calls", []),
        }
        return response


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
    from app.audit.local_trace import scope

    with (
        scope(
            "tool", call["name"], input=call.get("args"), tool_name=call["name"]
        ) as observation,
        tracing_context(**context),
    ):
        result = await _tool_call(tool, call, langsmith_extra={"name": call["name"]})
        observation["output"] = (
            result.content
            if call["name"] != "view_image"
            else {
                "path": call.get("args", {}).get("path"),
                "message": "图片已提供给模型，二进制不记录",
            }
        )
        if getattr(result, "status", None) == "error":
            observation["status"] = "failed"
        elif call["name"] == "report_document" and call.get("args", {}).get(
            "action"
        ) in {"write", "edit"}:
            with scope(
                "report", "报告已保存", input={"action": call["args"]["action"]}
            ):
                pass
        return result
