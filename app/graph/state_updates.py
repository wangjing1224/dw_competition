"""校验批量审核 Decision、推进文件游标并确定性汇总结果。"""

from typing import Any

from langgraph.graph.message import REMOVE_ALL_MESSAGES, RemoveMessage

from app.graph.audit_state import AuditState, current_file
from app.graph.routing import can_parse_decision
from app.llm.structured_output import extract_json_value
from app.models.findings import Finding
from app.models.results import AuditResult, FileAuditResult, ProjectMatch
from app.models.tasks import BatchAuditDecision
from app.storage.ids import next_id
from app.storage.repositories import AuditEventRepository


CONCLUSION_ORDER = {
    "non_compliant": 0,
    "risk": 1,
    "manual_review": 2,
    "compliant": 3,
}


def summarize_conclusion(findings: list[Finding]) -> str:
    if not findings:
        return "manual_review"
    return min((str(item.result) for item in findings), key=CONCLUSION_ORDER.__getitem__)


def _fallback_decision(state: AuditState) -> BatchAuditDecision:
    bid_file = current_file(state)
    findings = [
        Finding(
            finding_id="pending",
            task_id=state["task_id"],
            project_id=state["project_id"],
            bidder_id=state["bidder_id"],
            bid_file_id=bid_file.file_id,
            rule_id=rule.rule_id,
            response_status="unreadable",
            category=rule.category,
            severity=rule.severity,
            result="manual_review",
            title=rule.name,
            description="主 Agent 达到步数上限或未返回合法批量审核结果",
            rule_source=rule.source,
            suggestion="人工核对该规则及投标响应",
            confidence=0,
        )
        for rule in state["rule_set"].rules
    ]
    return BatchAuditDecision(
        action="record_batch",
        reason="自动批量审核未能可靠完成",
        file_id=bid_file.file_id,
        summary="当前文件需要人工复核",
        findings=findings,
    )


def apply_bid_agent_output_node(
    state: AuditState,
    *,
    event_repository: AuditEventRepository | None = None,
) -> dict[str, Any]:
    last = state["messages"][-1] if state["messages"] else None
    decision = (
        BatchAuditDecision.model_validate(extract_json_value(last.content))
        if last is not None and can_parse_decision(last)
        else _fallback_decision(state)
    )
    bid_file = current_file(state)
    if decision.file_id != bid_file.file_id:
        raise ValueError("批量审核结果不属于当前投标文件")

    expected = {rule.rule_id for rule in state["rule_set"].rules}
    actual = {finding.rule_id for finding in decision.findings}
    if expected != actual or len(actual) != len(decision.findings):
        raise ValueError("批量审核结果必须且只能覆盖每条规则一次")

    findings: list[Finding] = []
    rules_by_id = {rule.rule_id: rule for rule in state["rule_set"].rules}
    for finding in decision.findings:
        identity = (
            finding.task_id,
            finding.project_id,
            finding.bidder_id,
            finding.bid_file_id,
        )
        expected_identity = (
            state["task_id"],
            state["project_id"],
            state["bidder_id"],
            bid_file.file_id,
        )
        if identity != expected_identity:
            raise ValueError("Finding 归属与当前文件不一致")
        rule = rules_by_id[finding.rule_id]
        if (
            str(finding.category) != str(rule.category)
            or str(finding.severity) != str(rule.severity)
        ):
            raise ValueError("Finding 类别或风险等级与 RuleSet 不一致")
        findings.append(
            finding.model_copy(update={"finding_id": next_id("finding")})
        )

    known_result_ids: set[str] = set()
    evidence_results = []
    for result in decision.evidence_results:
        identity = (
            result.task_id,
            result.project_id,
            result.bidder_id,
            result.bid_file_id,
        )
        if identity != (
            state["task_id"],
            state["project_id"],
            state["bidder_id"],
            bid_file.file_id,
        ):
            raise ValueError("EvidenceReviewResult 归属与当前文件不一致")
        if result.rule_id not in expected:
            raise ValueError("EvidenceReviewResult 引用了未知规则")
        if result.evidence_result_id in known_result_ids:
            continue
        known_result_ids.add(result.evidence_result_id)
        evidence_results.append(result)

    file_result = FileAuditResult(
        file=bid_file,
        is_applicable=True,
        project_match=ProjectMatch(
            status="matched",
            reason=state["bid_summaries"][bid_file.file_id].match_reason,
        ),
        document_summary=state["bid_summaries"][bid_file.file_id],
        overall_conclusion=summarize_conclusion(findings),
        findings=findings,
        evidence_results=evidence_results,
        suggestions=list(
            dict.fromkeys(item.suggestion for item in findings if item.suggestion)
        ),
    )
    if event_repository is not None:
        event_repository.append(
            state["task_id"],
            actor="main_agent",
            event_type="agent_decision",
            title="主 Agent 完成当前文件批量审核",
            detail={
                "file_id": bid_file.file_id,
                "finding_count": len(findings),
                "evidence_result_count": len(evidence_results),
                "overall_conclusion": str(file_result.overall_conclusion),
            },
        )
    return {
        "messages": [RemoveMessage(id=REMOVE_ALL_MESSAGES)],
        "findings": [*state["findings"], *findings],
        "evidence_results": [*state["evidence_results"], *evidence_results],
        "file_results": [*state["file_results"], file_result],
        "current_bid_index": state["current_bid_index"] + 1,
        "grouped_retrieval_context": {},
        "react_step_count": 0,
    }


def build_audit_result_node(state: AuditState) -> dict[str, AuditResult]:
    by_file = {item.file.file_id: item for item in state["file_results"]}
    ordered_ids = [
        *state["bid_summaries"].keys(),
    ]
    return {
        "audit_result": AuditResult(
            task_id=state["task_id"],
            project_id=state["project_id"],
            bidder_id=state["bidder_id"],
            file_results=[by_file[file_id] for file_id in ordered_ids],
        )
    }
