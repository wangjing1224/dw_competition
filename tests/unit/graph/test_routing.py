"""主 Agent 结果和截断重试路由测试。"""

from langchain_core.messages import AIMessage

from app.graph.routing import route_bid_agent
from app.models.rules import RuleSet
from app.models.tasks import AgentAuditDecision


def _state(message):
    return {
        "messages": [message],
        "react_step_count": 1,
        "length_retry_count": 0,
        "format_retry_count": 0,
        "rule_set": RuleSet(project_id="project-1"),
    }


def test_valid_decision_is_applied():
    decision = AgentAuditDecision(summary="完成", categories=[])
    assert route_bid_agent(
        _state(AIMessage(content=decision.model_dump_json())),
        8,
    ) == "apply_output"


def test_length_response_gets_one_compact_retry():
    message = AIMessage(
        content='{"summary":"截断"',
        response_metadata={"finish_reason": "length"},
    )
    assert route_bid_agent(_state(message), 8) == "retry_length"
