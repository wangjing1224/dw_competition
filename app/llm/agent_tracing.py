"""保留 Agent 会话的模型/工具诊断，但不把图片二进制上传到 Trace。"""

import json
import logging
import re
from time import perf_counter
from uuid import uuid4

from langchain_core.messages import BaseMessage, ToolMessage
from langsmith import traceable, tracing_context
from pydantic import ValidationError

from app.llm.model_diagnostics import request_diagnostics, response_diagnostics

logger = logging.getLogger(__name__)


def pending_tool_calls(response):
    """保留合法和非法调用的关联；非法参数不修补、不交给工具执行。"""
    calls = [*response.tool_calls, *response.invalid_tool_calls]
    ids = [call.get("id") for call in calls]
    if any(not key for key in ids) or len(ids) != len(set(ids)):
        raise RuntimeError("工具调用 ID 缺失或重复，无法安全关联工具结果")
    if any(not call.get("name") for call in calls):
        raise RuntimeError("工具调用缺少名称，无法安全返回工具结果")
    # SDK 将合法/非法调用分开存储；有原始顺序时沿用它，不修改 AI 消息。
    order = {
        call.get("id"): position
        for position, call in enumerate(
            response.additional_kwargs.get("tool_calls", [])
        )
    }
    return sorted(calls, key=lambda call: order.get(call["id"], len(order)))


def check_tool_arguments(call):
    """只为解析失败生成简短诊断，不回显原始参数或 SDK 的整段错误正文。"""
    if call.get("type") != "invalid_tool_call":
        return
    detail = "参数必须是合法 JSON 对象"
    try:
        json.loads(call.get("args") or "")
    except json.JSONDecodeError as exc:
        detail = f"JSON 解析失败：{exc.msg}（第 {exc.lineno} 行，第 {exc.colno} 列）"
    except (ValueError, TypeError, RecursionError):
        pass
    raise ValueError(
        f"工具未执行，本次调用未修改数据。{detail}；请按参数 Schema 修正后重新调用。"
    )


def tool_error_text(exc):
    """工具错误反馈保留原因，省去参数回显、凭据、服务 URL 和磁盘绝对路径。"""
    if isinstance(exc, ValidationError):
        detail = "；".join(
            f"{'.'.join(map(str, error['loc'])) or '参数'}: {error['msg']}"
            for error in exc.errors(
                include_input=False, include_context=False, include_url=False
            )
        )
    else:
        detail = str(exc)
    detail = re.sub(r"(?i)(?:sk-|lsv2_)[A-Za-z0-9_+=/-]{6,}", "[密钥已隐藏]", detail)
    detail = re.sub(
        r"(?i)(?:bearer\s+|(?:api[_-]?key|authorization|token|password)\s*[:=]\s*)[^\s,;]+",
        "[凭据已隐藏]",
        detail,
    )
    detail = re.sub(r"https?://[^\s\"'<>]+", "[服务地址已隐藏]", detail)
    detail = re.sub(
        r"(?<![\w/])(?:/[\w.~-]+|[A-Za-z]:[\\/])(?:[^\s\"'<>]*)",
        "[本地路径已隐藏]",
        detail,
    )
    return f"{type(exc).__name__}: {detail}"


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
        if not isinstance(result, ToolMessage) or result.tool_call_id != call["id"]:
            # 由调用边界在普通异常捕获之外终止；诊断不能先因读 content 而报错。
            observation.update(status="failed", summary="工具返回无法关联当前调用")
            return result
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
