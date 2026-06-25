"""BaseTool 对模型隐藏图状态，只暴露业务参数。"""

import pytest
from langchain_core.messages import AIMessage
from langgraph.graph import END, StateGraph
from langgraph.prebuilt import ToolNode

from app.graph.audit_state import AuditState
from app.models.documents import DocumentRef, SourceRef
from app.models.evidence import EvidenceCatalogItem
from app.models.rules import RuleItem, RuleSet
from app.retrieval.models import SearchResult
from app.retrieval.service import RetrievalService
from app.storage.repositories import AuditEventRepository
from app.tools.bid_tools import (
    EvidenceReviewCheck,
    EvidenceReviewToolInput,
    RequestEvidenceReviewTool,
    SearchBidResponseTool,
    SearchBidResponseInput,
    SearchTenderSourceInput,
)
from app.tools.evidence_tools import SearchEvidenceInput, SearchEvidenceTool


def test_search_schema_requires_grouped_checks():
    bid_schema = SearchBidResponseInput.model_json_schema()
    tender_schema = SearchTenderSourceInput.model_json_schema()
    assert "checks" in bid_schema["properties"]
    assert bid_schema["properties"]["checks"]["description"]
    assert bid_schema["properties"]["checks"]["maxItems"] == 8
    assert bid_schema["$defs"]["BidSearchCheck"]["properties"]["queries"][
        "maxItems"
    ] == 5
    assert tender_schema["properties"]["checks"]["description"]
    assert tender_schema["properties"]["checks"]["maxItems"] == 8
    assert tender_schema["$defs"]["TenderSearchCheck"]["properties"]["queries"][
        "maxItems"
    ] == 5
    assert (
        bid_schema["properties"]["checks"]["description"]
        != tender_schema["properties"]["checks"]["description"]
    )


def test_injected_state_is_hidden_from_tool_call_schema():
    schema = SearchBidResponseTool.model_construct().tool_call_schema.model_json_schema()
    assert set(schema["properties"]) == {"checks"}
    # 输入 DTO 中保留状态注入点，但模型实际看到的工具 Schema 不包含它。
    assert "state" in SearchBidResponseInput.model_fields
    evidence_schema = (
        SearchEvidenceTool.model_construct().tool_call_schema.model_json_schema()
    )
    assert set(evidence_schema["properties"]) == {"checks", "file_ids"}


def test_evidence_review_input_is_business_only_except_injected_state():
    assert set(EvidenceReviewToolInput.model_fields) == {
        "checks",
        "state",
    }
    schema = EvidenceReviewToolInput.model_json_schema()
    assert schema["properties"]["checks"]["description"]
    assert schema["properties"]["checks"]["maxItems"] == 10


def test_evidence_search_can_optionally_limit_files():
    assert set(SearchEvidenceInput.model_fields) == {
        "checks",
        "file_ids",
        "state",
    }
    schema = SearchEvidenceInput.model_json_schema()
    assert schema["properties"]["checks"]["description"]
    assert schema["properties"]["file_ids"]["description"]
    assert schema["$defs"]["EvidenceSearchCheck"]["properties"]["queries"][
        "maxItems"
    ] == 5


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
                        "args": {
                            "checks": [
                                {
                                    "topic": "探测器分辨率",
                                    "queries": ["探测器分辨率"],
                                }
                            ]
                        },
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
    # 使用真实 AuditState，防止输入模型前向引用导致 InjectedState 失效。
    graph = StateGraph(AuditState)
    graph.add_node("tools", ToolNode([tool]))
    graph.set_entry_point("tools")
    graph.add_edge("tools", END)
    output = await graph.compile().ainvoke(state)
    tool_message = output["messages"][-1]
    assert "chunk-1-1-1-1" in tool_message.content
    assert '"topic": "探测器分辨率"' in tool_message.content


class UnexpectedEvidenceGraph:
    async def ainvoke(self, *_args, **_kwargs):
        raise AssertionError("只有人工读取资料时不应启动子 Agent")


@pytest.mark.asyncio
async def test_evidence_review_batch_preserves_rule_and_category(temp_settings):
    tool = RequestEvidenceReviewTool(
        evidence_graph=UnexpectedEvidenceGraph(),
        settings=temp_settings,
        event_repository=NoopEvents(),
    )
    rule = RuleItem(
        rule_id="rule-1",
        category="qualification",
        requirement="投标人应提供有效营业执照",
        priority="critical",
        verification="evidence_check",
        evidence_types=["business_license"],
        source=SourceRef(
            file_id="rule-file",
            file_name="招标文件.docx",
            source_path="招标文件.docx",
        ),
    )
    response = await tool._arun(
        checks=[
            EvidenceReviewCheck(
                rule_id="rule-1",
                evidence_type="business_license",
                verification_request="核验营业执照主体名称和经营状态",
            )
        ],
        state={
            "task_id": "audit-1",
            "project_id": "project-1",
            "bidder_id": "bidder-1",
            "rule_set": RuleSet(project_id="project-1", rules=[rule]),
            "evidence_catalog": {
                "business_license": [
                    EvidenceCatalogItem(
                        file_id="file-1",
                        status="manual_review",
                        summary="营业执照正文为图片",
                    )
                ]
            },
        },
    )
    assert response == {
        "results": [
            {
                "rule_id": "rule-1",
                "evidence_type": "business_license",
                "status": "ocr_required",
                "summary": "当前类别资料存在，但正文不可自动读取，需要人工核验",
                "evidence_chunk_ids": [],
            }
        ]
    }
