"""BaseTool 对模型隐藏图状态，只暴露业务参数。"""

import pytest
from langchain_core.messages import AIMessage
from langgraph.graph import END, StateGraph
from langgraph.prebuilt import ToolNode

from app.models.documents import DocumentRef, SourceRef
from app.retrieval.models import SearchResult
from app.retrieval.service import RetrievalService
from app.storage.repositories import AuditEventRepository
from app.tools.bid_tools import (
    EvidenceReviewToolInput,
    SearchBidResponseTool,
    SearchQueriesInput,
)
from app.tools.evidence_tools import SearchEvidenceInput


def test_search_schema_requires_queries():
    schema = SearchQueriesInput.model_json_schema()
    assert "queries" in schema["properties"]


def test_injected_state_is_hidden_from_tool_call_schema():
    schema = SearchBidResponseTool.model_construct().tool_call_schema.model_json_schema()
    assert set(schema["properties"]) == {"queries"}
    # 输入 DTO 中保留状态注入点，但模型实际看到的工具 Schema 不包含它。
    assert "state" in SearchQueriesInput.model_fields


def test_evidence_review_input_is_business_only_except_injected_state():
    assert set(EvidenceReviewToolInput.model_fields) == {
        "rule_id",
        "verification_request",
        "evidence_type",
        "state",
    }


def test_evidence_search_can_optionally_limit_files():
    assert set(SearchEvidenceInput.model_fields) == {
        "queries",
        "file_ids",
        "state",
    }


class NoopEvents(AuditEventRepository):
    def append(self, *args, **kwargs):
        return None


class FakeRetrieval(RetrievalService):
    def __init__(self):
        pass

    async def search(self, _query, _filter, **_kwargs):
        return [
            SearchResult(
                chunk_id="chunk-1-1-1-1",
                score=0.9,
                source=SourceRef(
                    file_id="file-1",
                    chunk_id="chunk-1-1-1-1",
                    file_name="投标.md",
                    source_path="投标.md",
                    section="技术参数",
                ),
                content="探测器分辨率为640x480",
                project_id="project-1",
                bidder_id="bidder-1",
                task_id="audit-1",
            )
        ]


@pytest.mark.asyncio
async def test_tool_node_injects_state_without_model_identity_arguments(
    temp_settings,
):
    tool = SearchBidResponseTool(
        retrieval_service=FakeRetrieval(),
        settings=temp_settings,
        event_repository=NoopEvents(),
    )
    state = {
        "messages": [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "search_bid_response",
                        "args": {"queries": ["探测器分辨率"]},
                        "id": "call-1",
                        "type": "tool_call",
                    }
                ],
            )
        ],
        "task_id": "audit-1",
        "project_id": "project-1",
        "bidder_id": "bidder-1",
        "bid_files": [
            DocumentRef(
                file_id="file-1",
                file_name="投标.md",
                source_path="投标.md",
                data_role="bid_document",
                parse_status="parsed",
                project_id="project-1",
                bidder_id="bidder-1",
                task_id="audit-1",
            )
        ],
        "current_bid_index": 0,
    }
    graph = StateGraph(dict)
    graph.add_node("tools", ToolNode([tool]))
    graph.set_entry_point("tools")
    graph.add_edge("tools", END)
    output = await graph.compile().ainvoke(state)
    assert "chunk-1-1-1-1" in output["messages"][0].content
