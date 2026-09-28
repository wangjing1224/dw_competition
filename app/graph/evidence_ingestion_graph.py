"""资料入库会话：调用前限制轮数，行为路由只执行工具或结束。"""

from functools import partial
from typing import TypedDict

from langchain_core.messages import BaseMessage, ToolMessage
from langgraph.graph import END, StateGraph

from app.agents.evidence_ingestion_agent import evidence_ingestion_node
from app.evidence.local_trace import close_scope, observe_tool_result, trace_scope
from app.llm.agent_tracing import invoke_agent_tool


class EvidenceIngestionState(TypedDict):
    messages: list[BaseMessage]
    model_calls: int
    prepared_step: int | None


def build_evidence_ingestion_graph(
    *, model, tools, session, controller, max_steps, trace_context
):
    by_name = {tool.name: tool for tool in tools}

    async def execute_tools(state):
        messages = list(state["messages"])
        if state["model_calls"] >= max_steps:
            raise RuntimeError("模型在未绑定工具的最终轮仍发出工具调用")
        # 顺序执行同一轮的调用，避免保存和查看发生竞态；每个 ID 都补齐结果。
        for call in messages[-1].tool_calls:
            name, call_id = call["name"], call["id"]
            if not call_id:
                raise ValueError("工具调用缺少 tool_call_id")
            try:
                before = next(
                    (
                        m.model_dump(mode="json")
                        for m in session.library.materials
                        if m.file_id == session.file_id
                    ),
                    None,
                )
                title = (
                    (
                        "查看历史摘要"
                        if call.get("args", {}).get("material") is None
                        else "保存当前资料"
                    )
                    if name == "save_evidence_material"
                    else name
                )
                with trace_scope(
                    session,
                    "tool",
                    title,
                    input=call.get("args"),
                    name=name,
                    tool_call_id=call_id,
                ) as observation:
                    if name not in by_name:
                        raise ValueError(f"未知工具 {name}")
                    result = await invoke_agent_tool(by_name[name], call, trace_context)
                    if (
                        not isinstance(result, ToolMessage)
                        or result.tool_call_id != call_id
                    ):
                        raise ValueError("工具未返回匹配的 ToolMessage")
                    observe_tool_result(observation, session, call, result, before)
            except Exception as exc:  # noqa: BLE001 - 每个失败调用也必须补齐对应的 ToolMessage。
                detail = f"{type(exc).__name__}: {exc}"
                if name == "save_evidence_material":
                    args = call.get("args")
                    reading = (
                        isinstance(args, dict)
                        and "material" in args
                        and args["material"] is None
                    )
                    if reading:
                        detail = (
                            "查看失败，没有发起写入；纠正 file_ids 后重试。\n" + detail
                        )
                    else:
                        session.unresolved_write_error = detail
                        detail = (
                            "保存失败，候选内容未生效；纠正后重新提交完整结果。\n"
                            + detail
                        )
                else:
                    detail = "工具执行失败，不能视为资料不存在或图片不可读。\n" + detail
                result = ToolMessage(
                    content=detail, name=name, tool_call_id=call_id, status="error"
                )
            messages.append(result)
        close_scope(session, "turn")
        return {"messages": messages}

    graph = StateGraph(EvidenceIngestionState)
    graph.add_node(
        "agent",
        partial(
            evidence_ingestion_node,
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
    graph.add_conditional_edges(
        "agent",
        lambda state: (
            "tools" if getattr(state["messages"][-1], "tool_calls", None) else END
        ),
        {"tools": "tools", END: END},
    )
    graph.add_edge("tools", "agent")
    return graph.compile()
