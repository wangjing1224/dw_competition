"""规则 Agent 节点：轮次限制只在模型调用前判断，路由仅分发已产生的行为。"""

from langchain_core.messages import HumanMessage

from app.prompts.loader import render_prompt
from app.rules.tracing import invoke_rule_model


async def rule_agent_node(state, *, model, tools, session, max_steps, trace_context):
    step = state["model_calls"] + 1
    if step > max_steps:
        raise RuntimeError("规则 Agent 已结束，不能超出最大轮数再次调用")
    messages = list(state["messages"])
    if step == max_steps:
        messages.append(
            HumanMessage(
                content=render_prompt(
                    "rule_extraction_finalize",
                    current_step=step,
                    max_steps=max_steps,
                )
            )
        )
        runnable = model  # 不复用已 bind_tools 的 runnable。
    else:
        if step == max_steps - 1:
            messages.append(
                HumanMessage(
                    content=render_prompt(
                        "rule_extraction_last_tool_round",
                        current_step=step,
                        max_steps=max_steps,
                    )
                )
            )
        runnable = model.bind_tools(tools)
    session.model_calls = step
    response = await invoke_rule_model(
        runnable, messages, [] if step == max_steps else tools, trace_context
    )
    if getattr(response, "invalid_tool_calls", None):
        raise RuntimeError("模型返回无法解析的工具调用，当前文件未完成")
    if response.response_metadata.get("finish_reason") == "length":
        raise RuntimeError("模型输出被截断，当前文件未完成")
    messages.append(response)
    return {"messages": messages, "model_calls": step}
