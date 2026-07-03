"""组装并编译资料核验 ReAct 子图。"""

from functools import partial

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.tools import BaseTool
from langgraph.graph import END, StateGraph
from langgraph.prebuilt import ToolNode

from app.agents.evidence_subagent import (
    evidence_agent_node,
    finalize_evidence_review,
    route_evidence_agent,
)
from app.graph.evidence_state import EvidenceState
from app.storage.repositories import AuditEventRepository


def build_evidence_graph(
    model: BaseChatModel,
    tools: list[BaseTool],
    *,
    max_steps: int = 3,
    event_repository: AuditEventRepository | None = None,
):
    """构建 request_evidence_review 工具内部使用的资料核验子图。

    子图只服务一次资料核验请求。它的输出会作为 request_evidence_review 的
    ToolMessage 返回给主 Agent，主 Agent 不直接看到子图内部多轮消息。
    """

    events = event_repository or AuditEventRepository()
    graph = StateGraph(EvidenceState)
    graph.add_node(
        "agent",
        partial(
            evidence_agent_node,
            model=model,
            tools=tools,
            max_steps=max_steps,
            event_repository=events,
        ),
    )
    graph.add_node("tools", ToolNode(tools))
    graph.add_node(
        "finalize",
        partial(finalize_evidence_review, event_repository=events),
    )
    graph.set_entry_point("agent")
    graph.add_conditional_edges(
        "agent",
        route_evidence_agent,
        {
            "agent": "agent",
            "tools": "tools",
            "finalize": "finalize",
            "limit_reached": "finalize",
        },
    )
    # 子 Agent 只做“检索 -> 判断”的短循环；如果最终文本不是合法 JSON，
    # finalize 节点会兜底成 unverifiable，而不是继续无限重试。
    graph.add_edge("tools", "agent")
    graph.add_edge("finalize", END)
    return graph.compile()
