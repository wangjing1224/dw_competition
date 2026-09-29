"""保留 Agent 会话的模型/工具诊断，但不把图片二进制上传到 Trace。"""

import json
import logging
from time import perf_counter
from uuid import uuid4

from langchain_core.messages import BaseMessage
from langsmith import traceable, tracing_context

from app.llm.model_diagnostics import request_diagnostics, response_diagnostics

logger = logging.getLogger(__name__)


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
    runnable,
    messages,
    tools,
    context,
    *,
    trace_name="agent_model",
    diagnostic_context=None,
    diagnostics=None,
    **call_options,
):
    from app.audit.local_trace import scope

    request = request_diagnostics(
        messages,
        tools,
        call_options,
        {
            **(context.get("metadata") or {}),
            **(diagnostic_context or {}),
        },
    )
    # 应用层调用 ID 用来配对并发请求；不冒充服务端 request_id。
    request.update(call_id=uuid4().hex, role=trace_name)
    logger.info("agent_model_request %s", json.dumps(request, ensure_ascii=False))
    started = perf_counter()
    with (
        scope(
            "model",
            "模型调用" if "compaction" not in trace_name else "上下文压缩模型",
            input=request,
        ) as observation,
        tracing_context(**context),
    ):
        try:
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
        except Exception as exc:
            logger.warning(
                "agent_model_call_failed %s",
                json.dumps(
                    {
                        **request,
                        "elapsed_ms": round((perf_counter() - started) * 1000),
                        "error_type": type(exc).__name__,
                    },
                    ensure_ascii=False,
                ),
            )
            raise  # 仅诊断，不增加重试或改变异常处理。
        try:
            detail = response_diagnostics(response)
        except Exception as exc:  # noqa: BLE001 - 旁路诊断失败不得中断模型业务，也不输出可能含正文的异常。
            detail = {"diagnostics_error": type(exc).__name__}
        detail = {
            **request,
            **detail,
            "elapsed_ms": round((perf_counter() - started) * 1000),
        }
        abnormal = (
            bool(getattr(response, "invalid_tool_calls", None))
            or response.response_metadata.get("finish_reason") == "length"
        )
        logger.log(
            logging.WARNING if abnormal else logging.INFO,
            "agent_model_response %s",
            json.dumps(detail, ensure_ascii=False),
        )
        if diagnostics is not None:
            diagnostics.update(detail)
        # 仅可见正文和工具调用；不返回请求 messages/System/Human。
        observation["output"] = {
            "content": response.content,
            "tool_calls": getattr(response, "tool_calls", []),
            "diagnostics": detail,
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
