"""主 Agent 工具、轻量结果和一次受控重试路由。"""

from langchain_core.messages import AIMessage

from app.graph.audit_state import AuditState
from app.llm.structured_output import extract_json_value
from app.models.tasks import AgentAuditDecision


def can_parse_decision(message: object) -> bool:
    if not isinstance(message, AIMessage) or not isinstance(message.content, str):
        return False
    try:
        AgentAuditDecision.model_validate(extract_json_value(message.content))
        return True
    except Exception:
        return False


def _finish_reason(message: object) -> str | None:
    if not isinstance(message, AIMessage):
        return None
    metadata = message.response_metadata or {}
    return metadata.get("finish_reason") or metadata.get("stop_reason")


def _decision_covers_state(state: AuditState, message: object) -> bool:
    if not can_parse_decision(message):
        return False
    decision = AgentAuditDecision.model_validate(
        extract_json_value(message.content)
    )
    expected = {
        rule.rule_id: str(rule.category) for rule in state["rule_set"].rules
    }
    actual: dict[str, str] = {}
    seen_categories: set[str] = set()
    for category in decision.categories:
        category_name = str(category.category)
        if category_name in seen_categories:
            return False
        seen_categories.add(category_name)
        for item in category.rules:
            if item.rule_id in actual:
                return False
            actual[item.rule_id] = category_name
    return actual == expected


def route_bid_agent(state: AuditState, max_steps: int) -> str:
    last = state["messages"][-1]
    if _decision_covers_state(state, last):
        return "apply_output"
    if state["react_step_count"] >= max_steps:
        return "limit_reached"
    if isinstance(last, AIMessage) and last.tool_calls:
        return "tools"
    retry_used = state["length_retry_count"] or state["format_retry_count"]
    if _finish_reason(last) == "length" and not retry_used:
        return "retry_length"
    if not retry_used:
        return "retry_format"
    return "limit_reached"
