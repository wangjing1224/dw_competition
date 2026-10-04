"""每个文件独立规则会话，按模型发出的顺序执行工具并闭合所有调用。"""

from functools import partial
from typing import TypedDict

from langchain_core.messages import BaseMessage, ToolMessage
from langgraph.errors import NodeCancelledError
from langgraph.graph import END, StateGraph

from app.agents.rule_agent import rule_agent_node
from app.llm.agent_tracing import (
    check_tool_arguments,
    pending_tool_calls,
    tool_error_text,
)
from app.llm.context_compaction import NeedsCompaction
from app.rules.local_trace import current_trace, observe_tool_result, trace_scope
from app.rules.tracing import invoke_rule_tool


class RuleState(TypedDict):
    messages: list[BaseMessage]
    model_calls: int
    prepared_step: int | None


def route_rule_agent(state):
    # 这里不检查轮次；有行为就路由到执行器，由节点负责调用前的工具控制。
    return "tools" if pending_tool_calls(state["messages"][-1]) else END


def build_rule_graph(
    *, model, tools, session, max_steps, controller, trace_context=None
):
    by_name = {tool.name: tool for tool in tools}
    trace_context = trace_context or {"enabled": False}

    async def execute_tools(state):
        messages = list(state["messages"])
        response = messages[-1]
        # 最终轮未绑定工具；若服务仍返回 tool_calls，视为协议异常，不继续循环。
        if state["model_calls"] >= max_steps:
            raise RuntimeError("模型在未提供工具的最终轮仍发出工具调用")
        for call in pending_tool_calls(response):
            name, call_id = call["name"], call["id"]
            try:
                before_profile = session.rule_set.project_profile
                commit_count = len(session.commits)
                with trace_scope(
                    session,
                    "tool",
                    name,
                    name=name,
                    tool_call_id=call_id,
                    input={
                        "arguments": call.get("args", {})
                        if call.get("type") != "invalid_tool_call"
                        else "[参数无法解析]"
                    },
                ) as observation:
                    check_tool_arguments(call)
                    if name not in by_name:
                        raise ValueError(f"未知工具 {name}")
                    result = await invoke_rule_tool(by_name[name], call, trace_context)
                    if (
                        isinstance(result, ToolMessage)
                        and result.tool_call_id == call_id
                    ):
                        observe_tool_result(
                            observation,
                            session,
                            call,
                            result,
                            before_profile,
                            commit_count,
                        )
                if (
                    name == "view_image"
                    and isinstance(result, ToolMessage)
                    and result.status != "error"
                ):
                    session.accessed_history.add(call["args"]["path"].split("/", 1)[0])
            except (NeedsCompaction, NodeCancelledError):
                raise
            except Exception as exc:  # noqa: BLE001 - 普通工具异常需闭合为对应的错误ToolMessage。
                detail = tool_error_text(exc)
                if name == "submit_rule_delta":
                    args = call.get("args")
                    # 只读意图按两个写入参数显式为 null 判定，不把误填 all
                    # 当成未完成写入。缺参/非法写入仍保守归入提交错误。
                    reading = isinstance(args, dict) and all(
                        key in args and args[key] is None
                        for key in ("operations", "project_profile")
                    )
                    if reading:
                        detail = (
                            "查看规则失败，没有发起写入。只读请重试："
                            '{"operations":null,"project_profile":null,"return_view":null}。\n'
                            + detail
                        )
                    else:
                        session.unresolved_write_error = detail
                        detail = (
                            "提交未正常完成，请核对当前规则视图后纠正；执行中失败不能保证没有落盘。\n"
                            + detail
                        )
                else:
                    detail = "工具未完成，不能视为无要求或图片不可读。\n" + detail
                result = ToolMessage(
                    content=detail, tool_call_id=call_id, name=name, status="error"
                )
            if not isinstance(result, ToolMessage) or result.tool_call_id != call_id:
                raise RuntimeError("工具未返回匹配当前调用的 ToolMessage")
            messages.append(result)
        if current_trace.get():
            current_trace.get().close_scope(session, "turn")
        return {"messages": messages}

    graph = StateGraph(RuleState)
    graph.add_node(
        "agent",
        partial(
            rule_agent_node,
            model=model,
            tools=tools,
            session=session,
            max_steps=max_steps,
            trace_context=trace_context,
            controller=controller,
        ),
    )
    graph.add_node("tools", execute_tools)
    graph.set_entry_point("agent")
    graph.add_conditional_edges("agent", route_rule_agent, {"tools": "tools", END: END})
    graph.add_edge("tools", "agent")
    return graph.compile()  # 无共享 checkpoint；每个文件都有全新 State。
