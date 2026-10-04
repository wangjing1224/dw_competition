"""审核主子会话共享调用前预算/轮次控制；业务权限和报告留在调用方。"""

from typing import TypedDict

from langchain_core.messages import BaseMessage, HumanMessage, ToolMessage
from langchain_core.utils.function_calling import convert_to_openai_tool
from langgraph.errors import NodeCancelledError
from langgraph.graph import END, StateGraph
from langsmith import tracing_context

from app.audit.local_trace import finish_turn, observed_session, observed_turn, turn
from app.llm.agent_tracing import (
    check_tool_arguments,
    invoke_agent_model,
    invoke_agent_tool,
    pending_tool_calls,
    tool_error_text,
)
from app.llm.context_compaction import (
    InputBudgetError,
    InputController,
    NeedsCompaction,
    compact_context,
)
from app.llm.factory import budgeted_generation_options
from app.llm.input_budget import InputContext
from app.llm.model_capabilities import get_model_capabilities
from app.llm.model_diagnostics import budget_diagnostics
from app.llm.platforms import ChatProtocol
from app.llm.token_counter import get_token_counter
from app.prompts.loader import render_prompt


class SessionState(TypedDict):
    messages: list[BaseMessage]
    model_calls: int
    prepared_step: int | None


def make_controller(settings):
    capabilities = get_model_capabilities(settings)
    return InputController(
        settings,
        get_token_counter(
            settings.tokenizer,
            image_tokens_per_image=capabilities.image_tokens_per_image,
        ),
        capabilities,
    )


def prepare_request(state, *, tools, max_steps, settings, role, missing=""):
    step = state["model_calls"] + 1
    if step > max_steps:
        raise RuntimeError("模型调用超过当前会话轮次上限")
    messages = list(state["messages"])
    if state.get("prepared_step") != step and step >= max_steps - 1:
        messages.append(
            HumanMessage(
                content=render_prompt(
                    f"{role}_{'finalize' if step == max_steps else 'last_tool_round'}",
                    current_step=step,
                    max_steps=max_steps,
                    missing=missing or "无",
                )
            )
        )
    bindings = (
        {"tools": [convert_to_openai_tool(tool) for tool in tools]}
        if step < max_steps
        else {}
    )
    return InputContext(tuple(messages), bindings, ChatProtocol.from_settings(settings))


@observed_turn
async def audit_agent_node(
    state,
    *,
    model,
    tools,
    controller,
    max_steps,
    role,
    limiter,
    trace_context,
    report=None,
    stats=None,
    events=None,
):
    context = prepare_request(
        state,
        tools=tools,
        max_steps=max_steps,
        settings=controller.settings,
        role=role,
        missing="、".join(report.missing()) if report else "",
    )
    decision = controller.check(context)
    if decision.status == "blocked":
        raise InputBudgetError(decision.reason)
    step = state["model_calls"] + 1
    if decision.status == "over_budget":
        if not any(message.type == "ai" for message in context.messages):
            raise InputBudgetError("固定输入超预算，没有可压缩交互")
        raise NeedsCompaction(
            {**state, "messages": list(context.messages), "prepared_step": step},
            context,
            decision,
        )
    active_tools = tools if step < max_steps else []
    if events:
        events(step, step == max_steps)
    # 名额只覆盖真实模型请求，等待工具或子会话时不占用。
    async with limiter:
        response = await invoke_agent_model(
            model.bind_tools(active_tools) if active_tools else model,
            list(context.messages),
            active_tools,
            trace_context,
            trace_name=role,
            diagnostic_context={**budget_diagnostics(controller), "turn_index": step},
            **budgeted_generation_options(
                controller.settings, controller.output_tokens(context)
            ),
        )
    controller.observe(context, response)
    if stats is not None:
        stats["model_calls"] = stats.get("model_calls", 0) + 1
    if response.response_metadata.get("finish_reason") not in (
        None,
        "stop",
        "tool_calls",
    ):
        raise RuntimeError("模型输出被截断或异常停止，保留草稿")
    return {
        "messages": [*context.messages, response],
        "model_calls": step,
        "prepared_step": None,
    }


def build_session_graph(
    *,
    model,
    tools,
    controller,
    max_steps,
    role,
    limiter,
    trace_context,
    report=None,
    stats=None,
    events=None,
):
    from functools import partial

    graph = StateGraph(SessionState)
    by_name = {tool.name: tool for tool in tools}

    async def execute_impl(state):
        messages = list(state["messages"])
        if state["model_calls"] >= max_steps:
            raise RuntimeError("模型在无工具末轮仍发出 tool_calls")
        for call in pending_tool_calls(messages[-1]):
            try:
                check_tool_arguments(call)
                if call["name"] not in by_name:
                    raise ValueError("未知工具，请使用当前绑定的工具名称")
                result = await invoke_agent_tool(
                    by_name[call["name"]], call, trace_context
                )
            except (NeedsCompaction, NodeCancelledError):
                raise  # 压缩信号、子图取消不是普通工具错误。
            except Exception as exc:  # noqa: BLE001 - 普通工具异常反馈模型，取消/协议故障另行终止。
                detail = tool_error_text(exc)
                if (
                    report
                    and call["name"] == "report_document"
                    and (
                        not isinstance(call.get("args"), dict)
                        or call["args"].get("action") != "read"
                    )
                ):
                    report.write_error = detail
                result = ToolMessage(
                    content=(
                        f"工具调用失败，不能作为业务结论：{detail}\n"
                        "请根据错误修正参数或检查当前结果后再调用；执行中失败不代表写入一定未生效。"
                    ),
                    name=call["name"],
                    tool_call_id=call["id"],
                    status="error",
                )
            # 无法关联的返回属于协议异常，不能当成可纠正的参数错误吞掉。
            if not isinstance(result, ToolMessage) or result.tool_call_id != call["id"]:
                raise RuntimeError("工具未返回对应 tool_call_id")
            messages.append(result)
        return {"messages": messages}

    async def execute(state):
        with turn(state["model_calls"]):
            result = await execute_impl(state)
            finish_turn()
            return result

    graph.add_node(
        "agent",
        partial(
            audit_agent_node,
            model=model,
            tools=tools,
            controller=controller,
            max_steps=max_steps,
            role=role,
            limiter=limiter,
            trace_context=trace_context,
            report=report,
            stats=stats,
            events=events,
        ),
    )
    graph.add_node("tools", execute)
    graph.set_entry_point("agent")
    graph.add_conditional_edges(
        "agent",
        lambda state: "tools" if pending_tool_calls(state["messages"][-1]) else END,
    )
    graph.add_edge("tools", "agent")
    return graph.compile()


@observed_session
async def run_session(
    *,
    model,
    tools,
    settings,
    max_steps,
    role,
    limiter,
    trace_context,
    build_messages,
    report=None,
    stats=None,
    events=None,
):
    controller = make_controller(settings)
    graph = build_session_graph(
        model=model,
        tools=tools,
        controller=controller,
        max_steps=max_steps,
        role=role,
        limiter=limiter,
        trace_context=trace_context,
        report=report,
        stats=stats,
        events=events,
    )
    state = {"messages": build_messages(""), "model_calls": 0, "prepared_step": None}
    compacted_steps = set()
    while True:
        try:
            with tracing_context(enabled=False):
                final = await graph.ainvoke(
                    state, config={"recursion_limit": max_steps * 2 + 2}
                )
            response = final["messages"][-1]
            if response.response_metadata.get("finish_reason") not in (None, "stop"):
                raise RuntimeError("模型未正常结束")
            if not isinstance(response.content, str) or not response.content.strip():
                raise RuntimeError("模型未返回非空处理结果")
            return response.content.strip()
        except NeedsCompaction as signal:
            step = signal.state["model_calls"] + 1
            if step in compacted_steps:
                raise InputBudgetError(
                    "同一轮压缩后仍超限，保留草稿，不无限重试"
                ) from signal
            compacted_steps.add(step)

            async def invoke(context, tokens, *, step=step):
                async with limiter:
                    return await invoke_agent_model(
                        model,
                        list(context.messages),
                        [],
                        trace_context,
                        trace_name=role + "_compaction",
                        diagnostic_context={
                            **budget_diagnostics(controller),
                            "turn_index": step,
                        },
                        **budgeted_generation_options(settings, tokens),
                    )

            compacted = await compact_context(
                context=signal.context,
                controller=controller,
                invoke=invoke,
                constraints="系统要求、规则、当前原文和已保存报告将重新提供。仅交接未保存判断、真实依据、已完成核验、失败查询及待办。不推进原文范围，不重算轮数，不代写报告。",
            )
            controller.reset_history()
            state = {
                "messages": build_messages(compacted.summary),
                "model_calls": signal.state["model_calls"],
                "prepared_step": None,
            }
            rebuilt = prepare_request(
                state,
                tools=tools,
                max_steps=max_steps,
                settings=settings,
                role=role,
                missing="、".join(report.missing()) if report else "",
            )
            checked = controller.check(rebuilt)
            if stats is not None:
                stats.setdefault("compactions", []).append(
                    {
                        "step": step,
                        "before": signal.decision.estimate.total,
                        "after": checked.estimate.total if checked.estimate else None,
                    }
                )
            if checked.status != "ready":
                raise InputBudgetError("重组后仍无法发送：" + checked.reason)
            state.update(messages=list(rebuilt.messages), prepared_step=step)
