"""主审核游标推进、重复 Finding 和总体结论测试。"""

from langchain_core.messages import AIMessage

from app.graph.audit_state import build_initial_audit_state
from app.graph.state_updates import apply_bid_agent_output_node, summarize_conclusion
from app.models.documents import DocumentRef, SourceRef
from app.models.findings import Finding
from app.models.results import ProjectMatch
from app.models.rules import RuleItem, RuleSet
from app.models.tasks import RecordFindingDecision, RecordProjectMatchDecision
from app.services.audit_task_service import AuditTaskContext


def source():
    return SourceRef(
        file_id="rule-file",
        file_name="规则.docx",
        source_path="规则.docx",
    )


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
        status="active",
        category="technical",
        name="像素",
        requirement="不低于640x480",
        check_type="semantic",
        severity="major",
        source=source(),
    )
    return build_initial_audit_state(
        AuditTaskContext(
            task_id="audit-1",
            project_id="project-1",
            bidder_id="bidder-1",
            rule_set=RuleSet(project_id="project-1", rules=[rule]),
            bid_files=[bid],
        )
    )


def test_project_match_then_finding_completes_file():
    current = state()
    match = RecordProjectMatchDecision(
        action="record_project_match",
        reason="匹配",
        project_match=ProjectMatch(status="matched", reason="项目一致"),
        is_applicable=True,
    )
    current["messages"] = [AIMessage(content=match.model_dump_json())]
    first = apply_bid_agent_output_node(current)
    current.update(first)

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
        title="未响应像素要求",
        description="没有找到响应",
        rule_source=source(),
        confidence=1,
    )
    decision = RecordFindingDecision(
        action="record_finding",
        reason="未找到",
        finding=finding,
    )
    current["messages"] = [AIMessage(content=decision.model_dump_json())]
    second = apply_bid_agent_output_node(current)

    assert second["current_bid_index"] == 1
    assert second["file_results"][0].overall_conclusion == "non_compliant"


def test_conclusion_priority_is_deterministic():
    base = state()
    finding = Finding(
        finding_id="finding-risk",
        task_id="audit-1",
        project_id="project-1",
        bidder_id="bidder-1",
        bid_file_id="bid-file",
        rule_id="rule-1",
        response_status="not_found",
        category="technical",
        severity="major",
        result="risk",
        title="风险",
        description="风险",
        rule_source=source(),
        confidence=1,
    )
    assert summarize_conclusion([finding]) == "risk"
