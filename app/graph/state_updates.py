"""校验主 Agent Decision、推进游标并确定性汇总结果。"""

from typing import Any

from langchain_core.messages import AIMessage
from langgraph.graph.message import REMOVE_ALL_MESSAGES, RemoveMessage

from app.graph.audit_state import (
    AuditState,
    applicable_rules_for_file,
    current_file,
    current_rule,
)
from app.graph.routing import can_parse_decision
from app.llm.structured_output import extract_json_value
from app.models.common import generate_id
from app.models.findings import Finding
from app.models.results import AuditResult, FileAuditResult, ProjectMatch
from app.models.tasks import (
    BidAgentDecisionAdapter,
    RecordFindingDecision,
    RecordProjectMatchDecision,
)
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


def build_file_result(
    state: AuditState,
    file_id: str,
) -> FileAuditResult:
    bid_file = next(item for item in state["bid_files"] if item.file_id == file_id)
    findings = [item for item in state["findings"] if item.bid_file_id == file_id]
    evidence = [
        item for item in state["evidence_results"] if item.bid_file_id == file_id
    ]
    return FileAuditResult(
        file=bid_file,
        is_applicable=True,
        project_match=state["project_matches"][file_id],
        overall_conclusion=summarize_conclusion(findings),
        findings=findings,
        evidence_results=evidence,
        suggestions=list(
            dict.fromkeys(
                item.suggestion for item in findings if item.suggestion
            )
        ),
    )


def _fallback_decision(state: AuditState) -> object:
    bid_file = current_file(state)
    if bid_file.file_id not in state["project_matches"]:
        return RecordProjectMatchDecision(
            action="record_project_match",
            reason="主 Agent 未在步数上限内完成适用性判断",
            project_match=ProjectMatch(
                status="uncertain",
                reason="自动适用性判断失败，继续规则审核并要求人工复核",
            ),
            is_applicable=True,
        )
    rules = applicable_rules_for_file(state["rule_set"], bid_file)
    if not rules:
        return None
    rule = current_rule(state)
    return RecordFindingDecision(
        action="record_finding",
        reason="主 Agent 未在步数上限内形成合法结论",
        finding=Finding(
            finding_id=generate_id("finding"),
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
            description="自动审核未能形成可靠结构化结论",
            rule_source=rule.source,
            suggestion="人工核对该规则及投标响应",
            confidence=0,
        ),
    )


def apply_bid_agent_output_node(
    state: AuditState,
    *,
    event_repository: AuditEventRepository | None = None,
) -> dict[str, Any]:
    last = state["messages"][-1] if state["messages"] else None
    if last is not None and can_parse_decision(last):
        decision = BidAgentDecisionAdapter.validate_python(
            extract_json_value(last.content)
        )
    else:
        decision = _fallback_decision(state)

    bid_file = current_file(state)
    project_matches = dict(state["project_matches"])
    findings = list(state["findings"])
    evidence_results = list(state["evidence_results"])
    file_results = list(state["file_results"])
    bid_index = state["current_bid_index"]
    rule_index = state["current_rule_index"]

    if event_repository is not None:
        event_repository.append(
            state["task_id"],
            actor="main_agent",
            event_type="agent_decision",
            title="主 Agent 形成审核决策",
            detail={
                "file_id": bid_file.file_id,
                "file_name": bid_file.file_name,
                "action": getattr(decision, "action", "no_rule"),
                "reason": getattr(decision, "reason", None),
                "rule_id": (
                    current_rule(state).rule_id
                    if bid_file.file_id in state["project_matches"]
                    and applicable_rules_for_file(state["rule_set"], bid_file)
                    else None
                ),
                "result": (
                    str(decision.finding.result)
                    if isinstance(decision, RecordFindingDecision)
                    else None
                ),
            },
        )

    if decision is None:
        project_match = project_matches[bid_file.file_id]
        file_results.append(
            FileAuditResult(
                file=bid_file,
                is_applicable=True,
                project_match=project_match,
                overall_conclusion="manual_review",
                suggestions=["当前文件没有可执行审核规则，请人工复核 RuleSet 适用范围"],
            )
        )
        bid_index += 1
        rule_index = 0
    elif decision.action == "record_project_match":
        if decision.project_match.status == "not_matched":
            file_results.append(
                FileAuditResult(
                    file=bid_file,
                    is_applicable=False,
                    project_match=decision.project_match,
                    exclusion_reason=decision.exclusion_reason,
                    overall_conclusion="not_applicable",
                )
            )
            bid_index += 1
        else:
            project_matches[bid_file.file_id] = decision.project_match
            if not applicable_rules_for_file(state["rule_set"], bid_file):
                file_results.append(
                    FileAuditResult(
                        file=bid_file,
                        is_applicable=True,
                        project_match=decision.project_match,
                        overall_conclusion="manual_review",
                        suggestions=["没有适用于当前文件的普通审核规则"],
                    )
                )
                bid_index += 1
        rule_index = 0
    elif decision.action == "record_file_result":
        if decision.file_result.file.file_id != bid_file.file_id:
            raise ValueError("FileAuditResult 不属于当前文件")
        file_results.append(decision.file_result)
        bid_index += 1
        rule_index = 0
    else:
        rule = current_rule(state)
        finding = decision.finding
        expected = (
            state["task_id"],
            state["project_id"],
            state["bidder_id"],
            bid_file.file_id,
            rule.rule_id,
        )
        actual = (
            finding.task_id,
            finding.project_id,
            finding.bidder_id,
            finding.bid_file_id,
            finding.rule_id,
        )
        if actual != expected:
            raise ValueError("Finding 归属或规则 ID 与当前游标不一致")
        if any(item.rule_id == finding.rule_id and item.bid_file_id == bid_file.file_id for item in findings):
            raise ValueError("同一文件和规则不能重复写入 Finding")
        findings.append(finding)
        evidence_results.extend(
            item
            for item in decision.evidence_results
            if item.evidence_result_id
            not in {existing.evidence_result_id for existing in evidence_results}
        )
        rule_index += 1
        if rule_index >= len(applicable_rules_for_file(state["rule_set"], bid_file)):
            temporary_state = {
                **state,
                "findings": findings,
                "evidence_results": evidence_results,
                "project_matches": project_matches,
            }
            file_results.append(build_file_result(temporary_state, bid_file.file_id))
            bid_index += 1
            rule_index = 0

    return {
        "messages": [RemoveMessage(id=REMOVE_ALL_MESSAGES)],
        "project_matches": project_matches,
        "findings": findings,
        "evidence_results": evidence_results,
        "file_results": file_results,
        "current_bid_index": bid_index,
        "current_rule_index": rule_index,
        "react_step_count": 0,
    }


def build_audit_result_node(state: AuditState) -> dict[str, AuditResult]:
    by_file = {item.file.file_id: item for item in state["file_results"]}
    ordered = [by_file[item.file_id] for item in state["bid_files"]]
    return {
        "audit_result": AuditResult(
            task_id=state["task_id"],
            project_id=state["project_id"],
            bidder_id=state["bidder_id"],
            file_results=ordered,
        )
    }
