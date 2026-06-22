"""使用固定模型响应执行一份文件、一条规则的完整主图。"""

from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage

from app.graph.audit_task_graph import LangGraphAuditExecutor
from app.models.documents import DocumentRef, SourceRef
from app.models.findings import Finding
from app.models.results import ProjectMatch
from app.models.rules import RuleItem, RuleSet
from app.models.tasks import RecordFindingDecision, RecordProjectMatchDecision
from app.services.audit_task_service import AuditTaskContext


class SequenceModel:
    def __init__(self, responses):
        self.responses = iter(responses)

    def bind_tools(self, _tools):
        return self

    async def ainvoke(self, _messages):
        return AIMessage(content=next(self.responses))


@pytest.mark.asyncio
async def test_main_graph_builds_audit_result(temp_settings):
    source = SourceRef(
        file_id="rule-file",
        file_name="规则.docx",
        source_path="规则.docx",
    )
    match = RecordProjectMatchDecision(
        action="record_project_match",
        reason="项目一致",
        project_match=ProjectMatch(status="matched", reason="项目一致"),
        is_applicable=True,
    )
    finding = Finding(
        finding_id="finding-1",
        task_id="audit-1",
        project_id="project-1",
        bidder_id="bidder-1",
        bid_file_id="bid-file",
        rule_id="rule-1",
        response_status="not_found",
        category="technical",
        severity="major",
        result="non_compliant",
        title="未响应",
        description="没有找到投标响应",
        rule_source=source,
        confidence=1,
    )
    decision = RecordFindingDecision(
        action="record_finding",
        reason="未响应",
        finding=finding,
    )
    model = SequenceModel([match.model_dump_json(), decision.model_dump_json()])
    models = SimpleNamespace(
        bid_chat=model,
        evidence_chat=SequenceModel([]),
        embedding=object(),
        rerank=object(),
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
                    status="active",
                    category="technical",
                    name="像素",
                    requirement="不低于640x480",
                    check_type="semantic",
                    severity="major",
                    source=source,
                )
            ],
        ),
        bid_files=[
            DocumentRef(
                file_id="bid-file",
                file_name="投标.docx",
                source_path="投标.docx",
                data_role="bid_document",
                parse_status="parsed",
                project_id="project-1",
                bidder_id="bidder-1",
                task_id="audit-1",
            )
        ],
    )

    result = await LangGraphAuditExecutor(temp_settings, models).run(context)

    assert len(result.file_results) == 1
    assert result.file_results[0].overall_conclusion == "non_compliant"
