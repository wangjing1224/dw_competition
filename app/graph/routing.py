"""批量主 Agent 工具路由和任务完成判断。"""

from langchain_core.messages import AIMessage

from app.graph.audit_state import AuditState
from app.llm.structured_output import extract_json_value
from app.models.tasks import BatchAuditDecision


def can_parse_decision(message: object) -> bool:
    if not isinstance(message, AIMessage) or not isinstance(message.content, str):
        return False
    try:
        BatchAuditDecision.model_validate(extract_json_value(message.content))
        return True
    except Exception:
        return False


def route_bid_agent(state: AuditState, max_steps: int) -> str:
    last = state["messages"][-1]
    if isinstance(last, AIMessage) and last.tool_calls:
        return "limit_reached" if state["react_step_count"] >= max_steps else "tools"
    if can_parse_decision(last):
        return "apply_output"
    return "limit_reached" if state["react_step_count"] >= max_steps else "bid_agent"


def route_after_state_update(state: AuditState) -> str:
    return (
        "complete"
        if state["current_bid_index"] >= len(state["bid_files"])
        else "continue"
    )


def can_build_audit_result(state: AuditState) -> bool:
    return state["current_bid_index"] >= len(state["bid_files"])
