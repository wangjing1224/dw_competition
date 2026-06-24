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


async def bid_agent_node(
    state: AuditState,
    *,
    model: BaseChatModel,
    tools: list[BaseTool],
    event_repository: AuditEventRepository,
) -> dict[str, Any]:
    bid_file = current_file(state)
    event_repository.append(
        state["task_id"],
        actor="main_agent",
        event_type="agent_turn_started",
        title="主 Agent 审核当前投标文件",
        detail={
            "step": state["react_step_count"] + 1,
            "file_id": bid_file.file_id,
            "file_name": bid_file.file_name,
            "rule_count": len(state["rule_set"].rules),
        },
    )
    response = await model.bind_tools(tools).ainvoke(build_bid_agent_messages(state))
    return {
        "messages": [response],
        "react_step_count": state["react_step_count"] + 1,
    }
