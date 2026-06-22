"""主审核 State、初始状态和当前文件/规则读取。"""

from typing import Annotated, TypedDict

from langchain_core.messages import BaseMessage
from langgraph.graph.message import add_messages

from app.models.documents import DocumentRef
from app.models.evidence import EvidenceReviewResult
from app.models.findings import Finding
from app.models.results import AuditResult, FileAuditResult, ProjectMatch
from app.models.rules import RuleItem, RuleSet
from app.models.tasks import TaskError
from app.services.audit_task_service import AuditTaskContext


class AuditState(TypedDict):
    messages: Annotated[list[BaseMessage], add_messages]
    task_id: str
    project_id: str
    bidder_id: str
    rule_set: RuleSet
    bid_files: list[DocumentRef]
    current_bid_index: int
    current_rule_index: int
    project_matches: dict[str, ProjectMatch]
    evidence_results: list[EvidenceReviewResult]
    findings: list[Finding]
    file_results: list[FileAuditResult]
    audit_result: AuditResult | None
    errors: list[TaskError]
    react_step_count: int


def build_initial_audit_state(context: AuditTaskContext) -> AuditState:
    return {
        "messages": [],
        "task_id": context.task_id,
        "project_id": context.project_id,
        "bidder_id": context.bidder_id,
        "rule_set": context.rule_set,
        "bid_files": context.bid_files,
        "current_bid_index": 0,
        "current_rule_index": 0,
        "project_matches": {},
        "evidence_results": [],
        "findings": [],
        "file_results": [],
        "audit_result": None,
        "errors": [],
        "react_step_count": 0,
    }


def current_file(state: AuditState) -> DocumentRef:
    return state["bid_files"][state["current_bid_index"]]


def applicable_rules_for_file(
    rule_set: RuleSet, _: DocumentRef
) -> list[RuleItem]:
    """包号等精细 scope 后续可扩展；当前排除专用于适用性判断的规则。"""

    return [
        rule
        for rule in rule_set.rules
        if str(rule.category) != "project_match"
    ]


def current_rule(state: AuditState) -> RuleItem:
    rules = applicable_rules_for_file(state["rule_set"], current_file(state))
    return rules[state["current_rule_index"]]
