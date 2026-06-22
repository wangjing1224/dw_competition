"""主 Agent 工具路由、状态推进路由和完成条件检查。"""

from langchain_core.messages import AIMessage

from app.graph.audit_state import AuditState, applicable_rules_for_file
from app.llm.structured_output import extract_json_value
from app.models.tasks import BidAgentDecisionAdapter


def can_parse_decision(message: object) -> bool:
    if not isinstance(message, AIMessage) or not isinstance(message.content, str):
        return False
    try:
        BidAgentDecisionAdapter.validate_python(extract_json_value(message.content))
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


def can_build_audit_result(state: AuditState) -> bool:
    return (
        state["current_bid_index"] == len(state["bid_files"])
        and len(state["file_results"]) == len(state["bid_files"])
    )


def check_completion(state: AuditState) -> bool:
    if not can_build_audit_result(state):
        return False
    for file_result in state["file_results"]:
        if not file_result.is_applicable:
            continue
        expected = applicable_rules_for_file(state["rule_set"], file_result.file)
        actual = {finding.rule_id for finding in file_result.findings}
        if {rule.rule_id for rule in expected} != actual:
            return False
        if any(not finding.rule_source.file_id for finding in file_result.findings):
            return False
    return True


def route_after_state_update(state: AuditState) -> str:
    return "complete" if check_completion(state) else "continue"
