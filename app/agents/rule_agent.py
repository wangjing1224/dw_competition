"""规则 Agent 节点：轮次限制只在模型调用前判断，路由仅分发已产生的行为。"""

from langchain_core.messages import HumanMessage
from langchain_core.utils.function_calling import convert_to_openai_tool

from app.llm.context_compaction import InputBudgetError, NeedsCompaction
from app.llm.factory import budgeted_generation_options
from app.llm.input_budget import InputContext
from app.llm.platforms import ChatProtocol
from app.prompts.loader import render_prompt
from app.rules.local_trace import current_trace, trace_scope
from app.rules.tracing import invoke_rule_model


def prepare_rule_request(state, *, tools, max_steps, model_settings):
    """先确定本轮工具与提醒，再核算；压缩续跑不得重复追加同轮提醒。"""
    step = state["model_calls"] + 1
    if step > max_steps:
        raise RuntimeError("规则 Agent 已结束，不能超出最大轮数再次调用")
    messages = list(state["messages"])
    if state.get("prepared_step") != step and step >= max_steps - 1:
        template = (
            "rule_extraction_finalize"
            if step == max_steps
            else "rule_extraction_last_tool_round"
        )
        messages.append(
            HumanMessage(
                content=render_prompt(
                    template,
                    current_step=step,
                    max_steps=max_steps,
                )
            )
        )
    bindings = (
        {"tools": [convert_to_openai_tool(tool) for tool in tools]}
        if step < max_steps
        else {}
    )
    return InputContext(
        tuple(messages), bindings, ChatProtocol.from_settings(model_settings)
    )


async def rule_agent_node(
    state, *, model, tools, session, max_steps, trace_context, controller
):
    step = state["model_calls"] + 1
    context = prepare_rule_request(
        state, tools=tools, max_steps=max_steps, model_settings=controller.settings
    )
    decision = controller.check(context)
    session.last_budget = {
        "status": decision.status,
        "input_tokens": decision.estimate.total if decision.estimate else None,
        "threshold": controller.budget.threshold,
        "method": decision.estimate.method if decision.estimate else None,
        "reason": decision.reason,
    }
    if decision.status == "blocked":
        raise InputBudgetError(decision.reason)
    if decision.status == "over_budget":
        # 组织策略在节点内判断；行为路由仍只处理模型已经发出的工具调用。
        if not any(message.type == "ai" for message in context.messages):
            raise InputBudgetError("固定输入已超预算且没有可交接的历史")
        raise NeedsCompaction(
            {**state, "messages": list(context.messages), "prepared_step": step},
            context,
            decision,
        )
    tokens = controller.output_tokens(context)
    runnable = model if step == max_steps else model.bind_tools(tools)
    session.model_calls = step
    session.file_model_calls += 1
    with trace_scope(
        session,
        "model",
        "模型调用",
        input={
            "model_name": controller.settings.model,
            "tools_enabled": step < max_steps,
            "tool_names": [tool.name for tool in tools] if step < max_steps else [],
        },
    ) as observation:
        response = await invoke_rule_model(
            runnable,
            list(context.messages),
            [] if step == max_steps else tools,
            trace_context,
            **budgeted_generation_options(controller.settings, tokens),
        )
        observation["output"] = {
            "content": response.content,
            "tool_calls": [
                {"id": c["id"], "name": c["name"], "arguments": c["args"]}
                for c in response.tool_calls
            ],
            "finish_reason": response.response_metadata.get("finish_reason"),
        }
    if not response.tool_calls and current_trace.get():
        current_trace.get().close_scope(session, "turn")
    controller.observe(context, response)
    if getattr(response, "invalid_tool_calls", None):
        raise RuntimeError("模型返回无法解析的工具调用，当前文件未完成")
    if response.response_metadata.get("finish_reason") == "length":
        raise RuntimeError("模型输出被截断，当前文件未完成")
    return {
        "messages": [*context.messages, response],
        "model_calls": step,
        "prepared_step": None,
    }
