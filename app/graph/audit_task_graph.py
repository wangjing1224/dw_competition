"""组装主审核 ReAct 图，并提供后台任务可注入的 AuditExecutor。"""

from functools import partial
from uuid import uuid4

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, StateGraph
from langgraph.prebuilt import ToolNode

from app.agents.bid_agent import bid_agent_node
from app.core.config import AppSettings
from app.graph.audit_state import AuditState, build_initial_audit_state
from app.graph.evidence_graph import build_evidence_graph
from app.graph.routing import route_bid_agent
from app.graph.state_updates import (
    apply_bid_agent_output_node,
    prepare_format_retry_node,
    prepare_length_retry_node,
)
from app.llm.factory import ModelRegistry
from app.models.results import AuditResult
from app.models.rules import RuleReportItem, present_rule_for_model
from app.retrieval.service import RetrievalService
from app.services.audit_task_service import AuditExecutor, AuditTaskContext
from app.storage.repositories import AuditEventRepository
from app.tools.bid_tools import (
    RequestEvidenceReviewTool,
    SearchBidResponseTool,
    SearchTenderSourceTool,
)
from app.tools.evidence_tools import SearchEvidenceTool


def build_audit_graph(
    settings: AppSettings,
    models: ModelRegistry,
    event_repository: AuditEventRepository | None = None,
):
    events = event_repository or AuditEventRepository()
    retrieval = RetrievalService(models.embedding, models.rerank, settings)
    evidence_tools = [
        SearchEvidenceTool(
            retrieval_service=retrieval,
            settings=settings,
            event_repository=events,
        )
    ]
    evidence_graph = build_evidence_graph(
        models.evidence_chat,
        evidence_tools,
        max_steps=settings.audit.evidence_agent_max_steps,
        event_repository=events,
    )
    bid_tools = [
        SearchBidResponseTool(
            retrieval_service=retrieval,
            settings=settings,
            event_repository=events,
        ),
        SearchTenderSourceTool(
            retrieval_service=retrieval,
            settings=settings,
            event_repository=events,
        ),
        RequestEvidenceReviewTool(
            evidence_graph=evidence_graph,
            settings=settings,
            event_repository=events,
        ),
    ]

    graph = StateGraph(AuditState)
    graph.add_node(
        "bid_agent",
        partial(
            bid_agent_node,
            model=models.bid_chat,
            tools=bid_tools,
            max_steps=settings.audit.bid_agent_max_steps,
            event_repository=events,
        ),
    )
    graph.add_node("tools", ToolNode(bid_tools))
    graph.add_node("retry_length", prepare_length_retry_node)
    graph.add_node("retry_format", prepare_format_retry_node)
    graph.add_node(
        "apply_output",
        partial(apply_bid_agent_output_node, event_repository=events),
    )
    graph.set_entry_point("bid_agent")
    graph.add_conditional_edges(
        "bid_agent",
        route_bid_agent,
        {
            "bid_agent": "bid_agent",
            "tools": "tools",
            "apply_output": "apply_output",
            "retry_length": "retry_length",
            "retry_format": "retry_format",
            "limit_reached": "apply_output",
        },
    )
    graph.add_edge("tools", "bid_agent")
    graph.add_edge("retry_length", "bid_agent")
    graph.add_edge("retry_format", "bid_agent")
    graph.add_edge("apply_output", END)
    return graph.compile(checkpointer=InMemorySaver())


class LangGraphAuditExecutor(AuditExecutor):
    def __init__(
        self,
        settings: AppSettings,
        models: ModelRegistry,
        event_repository: AuditEventRepository | None = None,
    ) -> None:
        self.graph = build_audit_graph(settings, models, event_repository)

    async def run(self, context: AuditTaskContext):
        file_results = list(context.routed_file_results)
        for bid_file in context.bid_files:
            summary = context.bid_summaries[bid_file.file_id]
            file_context = context.model_copy(
                update={
                    "bid_files": [bid_file],
                    "bid_summaries": {bid_file.file_id: summary},
                    "routed_file_results": [],
                }
            )
            # 每份 matched 文件拥有独立 State、checkpointer thread 和 LangSmith Trace。
            # 保持串行执行，避免未经验证的模型服务并发压力。
            trace_thread_id = (
                f"{context.task_id}-{bid_file.file_id}-{uuid4().hex[:12]}"
            )
            final_state = await self.graph.ainvoke(
                build_initial_audit_state(file_context),
                config={
                    "configurable": {"thread_id": trace_thread_id},
                    "run_name": "bid_audit_agent",
                    "tags": ["audit", "bid-agent"],
                    "metadata": {
                        "task_id": context.task_id,
                        "file_id": bid_file.file_id,
                        "file_name": bid_file.file_name,
                        "trace_thread_id": trace_thread_id,
                        "project_id": context.project_id,
                        "bidder_id": context.bidder_id,
                    },
                },
            )
            current_results = final_state.get("file_results") or []
            if len(current_results) != 1:
                raise ValueError(
                    f"主审核图未生成唯一文件结果: {bid_file.file_id}"
                )
            file_results.append(current_results[0])

        by_file = {item.file.file_id: item for item in file_results}
        return AuditResult(
            task_id=context.task_id,
            project_id=context.project_id,
            bidder_id=context.bidder_id,
            rules=[
                RuleReportItem.model_validate(
                    present_rule_for_model(rule).model_dump(mode="json")
                )
                for rule in context.rule_set.rules
            ],
            evidence_classifications=context.evidence_classifications,
            file_results=[
                by_file[file_id] for file_id in context.bid_summaries
            ],
        )
