"""组装主审核 ReAct 图，并提供后台任务可注入的 AuditExecutor。"""

from functools import partial

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, StateGraph
from langgraph.prebuilt import ToolNode

from app.agents.bid_agent import bid_agent_node
from app.core.config import AppSettings
from app.graph.audit_state import AuditState, build_initial_audit_state
from app.graph.evidence_graph import build_evidence_graph
from app.graph.routing import route_after_state_update, route_bid_agent
from app.graph.state_updates import (
    apply_bid_agent_output_node,
    build_audit_result_node,
)
from app.llm.factory import ModelRegistry
from app.retrieval.service import RetrievalService
from app.services.audit_task_service import AuditExecutor, AuditTaskContext
from app.storage.repositories import AuditTaskRepository, FileRepository
from app.tools.bid_tools import (
    RequestEvidenceReviewTool,
    RetrieveRuleSourceTool,
    SearchBidResponseTool,
)
from app.tools.evidence_tools import (
    RetrieveEvidenceSourceTool,
    SearchEvidenceTool,
)


def build_audit_graph(settings: AppSettings, models: ModelRegistry):
    """主图仅包含 Agent、Tool、状态更新和结果汇总四类节点。"""

    retrieval = RetrievalService(models.embedding, models.rerank, settings)
    evidence_tools = [
        SearchEvidenceTool(retrieval_service=retrieval),
        RetrieveEvidenceSourceTool(settings=settings),
    ]
    evidence_graph = build_evidence_graph(
        models.evidence_chat,
        evidence_tools,
        max_steps=8,
    )
    bid_tools = [
        SearchBidResponseTool(
            task_repository=AuditTaskRepository(),
            file_repository=FileRepository(),
            retrieval_service=retrieval,
        ),
        RetrieveRuleSourceTool(settings=settings),
        RequestEvidenceReviewTool(evidence_graph=evidence_graph),
    ]

    graph = StateGraph(AuditState)
    graph.add_node(
        "bid_agent",
        partial(bid_agent_node, model=models.bid_chat, tools=bid_tools),
    )
    graph.add_node("tools", ToolNode(bid_tools))
    graph.add_node("apply_output", apply_bid_agent_output_node)
    graph.add_node("build_result", build_audit_result_node)
    graph.set_entry_point("bid_agent")
    graph.add_conditional_edges(
        "bid_agent",
        partial(route_bid_agent, max_steps=10),
        {
            "bid_agent": "bid_agent",
            "tools": "tools",
            "apply_output": "apply_output",
            "limit_reached": "apply_output",
        },
    )
    graph.add_edge("tools", "bid_agent")
    graph.add_conditional_edges(
        "apply_output",
        route_after_state_update,
        {"continue": "bid_agent", "complete": "build_result"},
    )
    graph.add_edge("build_result", END)
    return graph.compile(checkpointer=InMemorySaver())


class LangGraphAuditExecutor(AuditExecutor):
    """每个服务实例编译一张图，thread_id 使用 task_id 防止状态串线。"""

    def __init__(self, settings: AppSettings, models: ModelRegistry) -> None:
        self.graph = build_audit_graph(settings, models)

    async def run(self, context: AuditTaskContext):
        final_state = await self.graph.ainvoke(
            build_initial_audit_state(context),
            config={"configurable": {"thread_id": context.task_id}},
        )
        result = final_state.get("audit_result")
        if result is None:
            raise ValueError("主审核图没有生成 AuditResult")
        return result
