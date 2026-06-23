"""主审核 Agent：一次处理当前投标文件的全部规则。"""

import json
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage
from langchain_core.tools import BaseTool

from app.audit.retrieval_context import group_rules
from app.graph.audit_state import AuditState, current_file
from app.models.tasks import BatchAuditDecision
from app.prompts.loader import render_prompt
from app.storage.repositories import AuditEventRepository


def _work_item(state: AuditState) -> dict[str, Any]:
    bid_file = current_file(state)
    return {
        "task_identity": {
            "task_id": state["task_id"],
            "project_id": state["project_id"],
            "bidder_id": state["bidder_id"],
        },
        "file": bid_file.model_dump(mode="json"),
        "document_summary": state["bid_summaries"][bid_file.file_id].model_dump(
            mode="json"
        ),
        "project_profile": state["rule_set"].project_profile.model_dump(mode="json"),
        "rule_groups": {
            category: [rule.model_dump(mode="json") for rule in rules]
            for category, rules in group_rules(state["rule_set"]).items()
        },
        "bid_evidence": {
            category: [item.model_dump(mode="json") for item in results]
            for category, results in state["grouped_retrieval_context"].items()
        },
        "evidence_catalog": {
            category: entry.model_dump(mode="json")
            for category, entry in state["evidence_catalog"].items()
        },
    }


def build_bid_agent_messages(state: AuditState) -> list[BaseMessage]:
    return [
        SystemMessage(
            content=render_prompt(
                "bid_agent",
                batch_audit_decision_schema=(
                    BatchAuditDecision.model_json_schema()
                ),
            )
        ),
        HumanMessage(
            content=(
                "当前文件批量审核输入：\n"
                f"{json.dumps(_work_item(state), ensure_ascii=False)}"
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
        title="主 Agent 批量审核当前投标文件",
        detail={
            "step": state["react_step_count"] + 1,
            "file_id": bid_file.file_id,
            "file_name": bid_file.file_name,
            "rule_count": len(state["rule_set"].rules),
        },
    )
    response = await model.bind_tools(tools).ainvoke(build_bid_agent_messages(state))
    if response.tool_calls:
        event_repository.append(
            state["task_id"],
            actor="main_agent",
            event_type="tool_plan",
            title="主 Agent 请求补充核验信息",
            detail={
                "step": state["react_step_count"] + 1,
                "tools": [
                    {"name": call.get("name"), "call_id": call.get("id")}
                    for call in response.tool_calls
                ],
            },
        )
    return {
        "messages": [response],
        "react_step_count": state["react_step_count"] + 1,
    }
