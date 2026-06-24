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
    events = event_repository or AuditEventRepository()
    graph = StateGraph(EvidenceState)
    graph.add_node(
        "agent",
        partial(
            evidence_agent_node,
            model=model,
            tools=tools,
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
        partial(route_evidence_agent, max_steps=max_steps),
        {
            "agent": "agent",
            "tools": "tools",
            "finalize": "finalize",
            "limit_reached": "finalize",
        },
    )
    graph.add_edge("tools", "agent")
    graph.add_edge("finalize", END)
    return graph.compile()
