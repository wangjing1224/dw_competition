"""主审核 Agent 的当前工作单元消息构造和模型节点。"""

import json
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langchain_core.tools import BaseTool

from app.graph.audit_state import (
    AuditState,
    applicable_rules_for_file,
    current_file,
    current_rule,
)
from app.models.tasks import BidAgentDecisionAdapter
from app.models.common import generate_id
from app.models.findings import Finding
from app.models.tasks import RecordFindingDecision
from app.prompts.loader import render_prompt


def _work_item(state: AuditState) -> dict[str, Any]:
    bid_file = current_file(state)
    if bid_file.file_id not in state["project_matches"]:
        match_rules = [
            rule
            for rule in state["rule_set"].rules
            if str(rule.category) == "project_match"
        ]
        return {
            "kind": "project_match",
            "file": bid_file.model_dump(mode="json"),
            "project_profile": state["rule_set"].project_profile.model_dump(
                mode="json"
            ),
            "search_rule": (
                match_rules[0].model_dump(mode="json") if match_rules else None
            ),
        }
    rules = applicable_rules_for_file(state["rule_set"], bid_file)
    if not rules:
        return {"kind": "no_applicable_rules", "file": bid_file.model_dump(mode="json")}
    return {
        "kind": "rule_audit",
        "file": bid_file.model_dump(mode="json"),
        "rule": current_rule(state).model_dump(mode="json"),
        "prior_findings": [
            item.model_dump(mode="json")
            for item in state["findings"]
            if item.bid_file_id == bid_file.file_id
        ],
    }


def build_bid_agent_messages(state: AuditState) -> list[BaseMessage]:
    content = (
        "任务身份："
        f"task_id={state['task_id']}, project_id={state['project_id']}, "
        f"bidder_id={state['bidder_id']}\n\n"
        f"当前工作单元：\n{json.dumps(_work_item(state), ensure_ascii=False)}"
    )
    return [
        SystemMessage(
            content=render_prompt(
                "bid_agent",
                bid_agent_decision_schema=(
                    BidAgentDecisionAdapter.json_schema()
                ),
            )
        ),
        HumanMessage(content=content),
        *state["messages"],
    ]


async def bid_agent_node(
    state: AuditState,
    *,
    model: BaseChatModel,
    tools: list[BaseTool],
) -> dict[str, Any]:
    bid_file = current_file(state)
    if bid_file.file_id in state["project_matches"]:
        rules = applicable_rules_for_file(state["rule_set"], bid_file)
        if rules:
            rule = current_rule(state)
            if str(rule.status) in {"rule_conflict", "manual_review"}:
                decision = RecordFindingDecision(
                    action="record_finding",
                    reason="规则本身存在冲突或需要人工复核，不执行普通响应判断",
                    finding=Finding(
                        finding_id=generate_id("finding"),
                        task_id=state["task_id"],
                        project_id=state["project_id"],
                        bidder_id=state["bidder_id"],
                        bid_file_id=bid_file.file_id,
                        rule_id=rule.rule_id,
                        response_status="unreadable",
                        category=rule.category,
                        severity=rule.severity,
                        result="manual_review",
                        title=rule.name,
                        description=rule.conflict_note or rule.requirement,
                        rule_source=rule.source,
                        suggestion="人工确认规则冲突和适用要求",
                        confidence=1,
                    ),
                )
                return {
                    "messages": [
                        AIMessage(content=decision.model_dump_json())
                    ],
                    "react_step_count": state["react_step_count"] + 1,
                }
    response = await model.bind_tools(tools).ainvoke(build_bid_agent_messages(state))
    return {
        "messages": [response],
        "react_step_count": state["react_step_count"] + 1,
    }
