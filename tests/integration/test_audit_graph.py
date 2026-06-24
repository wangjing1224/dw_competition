"""使用固定轻量响应执行一份文件、全部规则的完整主图。"""

from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage

from app.graph.audit_task_graph import LangGraphAuditExecutor
from app.models.documents import BidDocumentSummary, DocumentRef, SourceRef
from app.models.rules import RuleItem, RuleSet
from app.models.tasks import AgentAuditDecision
from app.services.audit_task_service import AuditTaskContext
from app.storage.repositories import AuditEventRepository


class NoopEventRepository(AuditEventRepository):
    def append(self, *args, **kwargs):
        return None


class SequenceModel:
    def __init__(self, responses):
        self.responses = iter(responses)

    def bind_tools(self, _tools):
        return self

    async def ainvoke(self, _messages):
        return AIMessage(content=next(self.responses))


@pytest.mark.asyncio
async def test_main_graph_builds_audit_result(temp_settings):
    decision = AgentAuditDecision(
        summary="技术规则未得到响应",
        categories=[
            {
                "category": "technical",
                "summary": "技术响应不完整",
                "rules": [
                    {
                        "rule_id": "rule-1",
                        "result": "non_compliant",
                        "summary": {"reason": "未找到分辨率响应"},
                    }
                ],
            }
        ],
    )
    models = SimpleNamespace(
        bid_chat=SequenceModel([decision.model_dump_json()]),
        evidence_chat=SequenceModel([]),
        embedding=object(),
        rerank=object(),
    )
    bid_file = DocumentRef(
        file_id="bid-file",
        file_name="投标.docx",
        source_path="投标.docx",
        data_role="bid_document",
        parse_status="parsed",
        project_id="project-1",
        bidder_id="bidder-1",
        task_id="audit-1",
    )
    source = SourceRef(
        file_id="rule-file",
        file_name="规则.docx",
        source_path="规则.docx",
    )
    context = AuditTaskContext(
        task_id="audit-1",
        project_id="project-1",
        bidder_id="bidder-1",
        rule_set=RuleSet(
            project_id="project-1",
            rules=[
                RuleItem(
                    rule_id="rule-1",
                    category="technical",
                    priority="critical",
                    verification="bid_only",
                    requirement="探测器分辨率不低于640x480",
                    source=source,
                )
            ],
        ),
        bid_files=[bid_file],
        bid_summaries={
            "bid-file": BidDocumentSummary(
                file_id="bid-file",
                summary="红外热成像仪投标文件",
                match_status="matched",
                match_reason="项目一致",
            )
        },
    )
    result = await LangGraphAuditExecutor(
        temp_settings,
        models,
        NoopEventRepository(),
    ).run(context)
    assert len(result.file_results) == 1
    assert result.file_results[0].overall_conclusion == "non_compliant"
    assert result.rules[0].requirement == "探测器分辨率不低于640x480"
