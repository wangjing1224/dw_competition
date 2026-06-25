"""主审核 Agent：读取轻量上下文并按需调用工具。"""

from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage
from langchain_core.tools import BaseTool

from app.graph.audit_state import AuditState, current_file
from app.models.rules import present_rule_for_model
from app.models.tasks import AgentAuditDecision
from app.prompts.loader import render_prompt
from app.storage.repositories import AuditEventRepository


def _group_rules(state: AuditState) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for rule in state["rule_set"].rules:
        item = present_rule_for_model(rule).model_dump(mode="json")
        category = str(item.pop("category"))
        grouped.setdefault(category, []).append(item)
    return grouped


def _catalog(state: AuditState) -> dict[str, list[dict[str, Any]]]:
    return {
        evidence_type: [item.model_dump(mode="json") for item in items]
        for evidence_type, items in state["evidence_catalog"].items()
    }


def build_bid_agent_messages(state: AuditState) -> list[BaseMessage]:
    bid_file = current_file(state)
    profile = state["rule_set"].project_profile
    return [
        SystemMessage(
            content=render_prompt(
                "bid_agent",
                agent_audit_decision_schema=(
                    AgentAuditDecision.model_json_schema()
                ),
            )
        ),
        HumanMessage(
            content=render_prompt(
                "bid_agent_input",
                project_name=profile.project_name or state["project_id"],
                bid_file=bid_file.file_name,
                bidder=state["bidder_id"],
                bid_summary=state["bid_summaries"][bid_file.file_id].summary,
                rule_groups=_group_rules(state),
                evidence_catalog=_catalog(state),
            )
        ),
        *state["messages"],
    ]


def build_force_finalize_message(current_step: int, max_steps: int) -> HumanMessage:
    """最后一轮不再提供工具，只允许基于已有上下文形成完整结果。"""

    return HumanMessage(
        content=(
            f"当前是第{current_step}轮，总轮次上限为{max_steps}轮，"
            "你已经达到当前投标文件的审核循环上限。禁止继续调用任何工具。"
            "请立即根据投标文件摘要、全部规则、资料目录和此前工具返回的有效信息，"
            "对每条规则形成最终判断。信息不足的规则使用manual_review，"
            "并在reason中简洁说明具体缺口。必须完整覆盖全部输入规则，"
            "只返回符合AgentAuditDecision Schema的JSON。"
        )
    )


async def bid_agent_node(
    state: AuditState,
    *,
    model: BaseChatModel,
    tools: list[BaseTool],
    max_steps: int,
    event_repository: AuditEventRepository,
) -> dict[str, Any]:
    bid_file = current_file(state)
    current_step = state["react_step_count"] + 1
    is_final_step = current_step >= max_steps
    event_repository.append(
        state["task_id"],
        actor="main_agent",
        event_type="agent_turn_started",
        title="主 Agent 审核当前投标文件",
        detail={
            "step": current_step,
            "max_steps": max_steps,
            "force_finalize": is_final_step,
            "file_id": bid_file.file_id,
            "file_name": bid_file.file_name,
            "rule_count": len(state["rule_set"].rules),
        },
    )
    messages = build_bid_agent_messages(state)
    if is_final_step:
        messages.append(build_force_finalize_message(current_step, max_steps))
        response = await model.ainvoke(messages)
    else:
        response = await model.bind_tools(tools).ainvoke(messages)
    return {
        "messages": [response],
        "react_step_count": current_step,
    }
