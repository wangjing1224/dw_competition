"""资料入库 Agent 节点：轮次限制只在模型调用前判断，路由仅分发已产生的行为。"""

from langchain_core.messages import HumanMessage
from langchain_core.utils.function_calling import convert_to_openai_tool

from app.evidence.local_trace import close_scope, trace_scope
from app.llm.agent_tracing import invoke_agent_model
from app.llm.context_compaction import InputBudgetError, NeedsCompaction
from app.llm.factory import budgeted_generation_options
from app.llm.input_budget import InputContext
from app.llm.model_diagnostics import budget_diagnostics
from app.llm.platforms import ChatProtocol
from app.prompts.loader import render_prompt


def prepare_evidence_request(state, *, tools, max_steps, model_settings):
    """先确定本轮工具与提醒，再核算；压缩续跑不得重复追加同轮提醒。"""
    step = state["model_calls"] + 1
    if step > max_steps:
        raise RuntimeError("资料入库 Agent 已结束，不能超出最大轮数再次调用")
    messages = list(state["messages"])
    if state.get("prepared_step") != step and step >= max_steps - 1:
        template = (
            "evidence_ingestion_finalize"
            if step == max_steps
            else "evidence_ingestion_last_tool_round"
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


async def evidence_ingestion_node(
    state, *, model, tools, session, max_steps, trace_context, controller
):
    step = state["model_calls"] + 1
    context = prepare_evidence_request(
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
    request_diagnostics = {
        **budget_diagnostics(controller),
        "bidder_id": session.library.bidder_id,
        "file_id": session.file_id,
        "batch_index": session.batch_index,
        "turn_index": step,
    }
    diagnostics = {}
    with trace_scope(
        session,
        "model",
        "模型调用",
        input={
            **request_diagnostics,
            "model": controller.settings.model,
            "tools": [tool.name for tool in tools] if step < max_steps else [],
        },
    ) as observation:
        response = await invoke_agent_model(
            runnable,
            list(context.messages),
            [] if step == max_steps else tools,
            trace_context,
            trace_name="evidence_ingestion",
            diagnostic_context=request_diagnostics,
            diagnostics=diagnostics,
            **budgeted_generation_options(controller.settings, tokens),
        )
        observation.update(
            output={
                "content": response.content,
                "tool_calls": response.tool_calls,
                "diagnostics": diagnostics,
            },
            summary="本轮返回工具调用"
            if response.tool_calls or response.invalid_tool_calls
            else "本轮返回正文",
        )
    controller.observe(context, response)
    # Task32 D2：暂时停用工具历史详情预算；统一输入控制器保持不变。
    # session.detail_budget_tokens = max(
    #     0,
    #     controller.budget.threshold
    #     - decision.estimate.total
    #     - controller.counter.count_text(str(response.content) + str(response.tool_calls))
    #     - 128,
    # )
    if response.response_metadata.get("finish_reason") == "length":
        raise RuntimeError("模型输出被截断，当前文件未完成")
    if not (response.tool_calls or response.invalid_tool_calls):
        close_scope(session, "turn")
    return {
        "messages": [*context.messages, response],
        "model_calls": step,
        "prepared_step": None,
    }
