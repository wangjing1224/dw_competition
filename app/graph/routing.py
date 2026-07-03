"""主 Agent 工具、轻量结果和一次受控重试路由。"""

from langchain_core.messages import AIMessage

from app.graph.audit_state import AuditState
from app.llm.structured_output import extract_json_value
from app.models.tasks import AgentAuditDecision


def can_parse_decision(message: object) -> bool:
    """只判断“这条消息是否像最终审核结果”，不做业务覆盖校验。"""

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
    """校验最终 JSON 是否完整覆盖当前文件需要审核的全部规则。

    主 Agent 输出采用“分类总结 + 分类内规则判断”的结构。这里要求：
    - 每个 category 只能出现一次；
    - 每条 rule_id 只能出现一次；
    - 输出 rule_id 集合和当前 RuleSet 完全一致；
    - rule_id 必须放在它所属的 category 下面。
    """

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


def route_bid_agent(state: AuditState) -> str:
    """主审核图的行为路由。

    这个函数只处理“上一条 AIMessage 已经产生之后，下一步该去哪”。
    不要在这里做最大轮次判断；最大轮次属于调用模型之前的策略控制，
    已经放在 bid_agent_node 中处理。

    路由优先级：
    1. 有 tool_calls：必须先执行 tools，闭合每个 tool_call_id。
    2. 已经是完整合法结果：进入 apply_output。
    3. 输出被 length 截断：给一次压缩重试。
    4. JSON/Schema 不合法：给一次格式修复重试。
    5. 仍失败：进入 apply_output，由输出节点生成人工复核兜底。
    """

    last = state["messages"][-1]
    if isinstance(last, AIMessage) and last.tool_calls:
        return "tools"
    if _decision_covers_state(state, last):
        return "apply_output"
    retry_used = state["length_retry_count"] or state["format_retry_count"]
    if _finish_reason(last) == "length" and not retry_used:
        return "retry_length"
    if not retry_used:
        return "retry_format"
    return "limit_reached"
