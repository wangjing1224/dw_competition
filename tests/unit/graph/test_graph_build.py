"""主图只含执行和工具，报告导出不作为图节点。"""

import asyncio

from app.audit.session import make_controller
from app.graph.audit_task_graph import build_audit_graph
from tests.helpers.audit import SequenceModel


def test_graph_compiles_without_external_models(temp_settings):
    graph = build_audit_graph(
        model=SequenceModel([]),
        tools=[],
        controller=make_controller(temp_settings.bid_chat_model),
        max_steps=8,
        limiter=asyncio.Semaphore(1),
        trace_context={},
    )
    assert set(graph.get_graph().nodes) == {"__start__", "agent", "tools", "__end__"}
