"""轻量审核结果覆盖、文件推进和总体结论测试。"""

from langchain_core.messages import AIMessage

from app.graph.audit_state import build_initial_audit_state
from app.graph.state_updates import apply_bid_agent_output_node, summarize_conclusion
from app.models.documents import BidDocumentSummary, DocumentRef, SourceRef
from app.models.findings import Finding
from app.models.rules import RuleItem, RuleSet
from app.models.tasks import AgentAuditDecision
from app.services.audit_task_service import AuditTaskContext


def state():
    bid = DocumentRef(
        file_id="bid-file",
        file_name="投标.docx",
        source_path="投标.docx",
        data_role="bid_document",
        parse_status="parsed",
        project_id="project-1",
        bidder_id="bidder-1",
        task_id="audit-1",
    )
    rule = RuleItem(
        rule_id="rule-1",
        category="technical",
        requirement="探测器分辨率不低于640x480",
        priority="critical",
        verification="bid_only",
        source=SourceRef(
            file_id="rule-file",
            file_name="规则.docx",
            source_path="规则.docx",
        ),
    )
    return build_initial_audit_state(
        AuditTaskContext(
            task_id="audit-1",
            project_id="project-1",
            bidder_id="bidder-1",
            rule_set=RuleSet(project_id="project-1", rules=[rule]),
            bid_files=[bid],
            bid_summaries={
                "bid-file": BidDocumentSummary(
                    file_id="bid-file",
                    summary="测试投标文件",
                    match_status="matched",
                    match_reason="项目一致",
                )
            },
        )
    )


def test_agent_decision_completes_file():
    current = state()
    decision = AgentAuditDecision(
        summary="存在未响应项",
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
    current["messages"] = [AIMessage(content=decision.model_dump_json())]
    updated = apply_bid_agent_output_node(current)
    assert updated["current_bid_index"] == 1
    assert updated["file_results"][0].overall_conclusion == "non_compliant"


def test_conclusion_priority_is_deterministic():
    finding = Finding(
        rule_id="rule-1",
        result="risk",
        summary="存在风险",
    )
    assert summarize_conclusion([finding]) == "risk"
