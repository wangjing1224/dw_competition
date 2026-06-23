"""批量主审核 State、初始状态和当前文件读取。"""

from typing import Annotated, TypedDict

from langchain_core.messages import BaseMessage
from langgraph.graph.message import add_messages

from app.models.documents import BidDocumentSummary, DocumentRef
from app.models.evidence import EvidenceCatalogEntry, EvidenceReviewResult
from app.models.findings import Finding
from app.models.results import AuditResult, FileAuditResult
from app.models.rules import RuleSet
from app.models.tasks import TaskError
from app.retrieval.models import SearchResult
from app.services.audit_task_service import AuditTaskContext


class AuditState(TypedDict):
    messages: Annotated[list[BaseMessage], add_messages]
    task_id: str
    project_id: str
    bidder_id: str
    rule_set: RuleSet
    bid_files: list[DocumentRef]
    bid_summaries: dict[str, BidDocumentSummary]
    evidence_catalog: dict[str, EvidenceCatalogEntry]
    current_bid_index: int
    grouped_retrieval_context: dict[str, list[SearchResult]]
    findings: list[Finding]
    evidence_results: list[EvidenceReviewResult]
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
        "bid_summaries": context.bid_summaries,
        "evidence_catalog": context.evidence_catalog,
        "current_bid_index": 0,
        "grouped_retrieval_context": {},
        "findings": [],
        "evidence_results": [],
        "file_results": list(context.routed_file_results),
        "audit_result": None,
        "errors": [],
        "react_step_count": 0,
    }


def current_file(state: AuditState) -> DocumentRef:
    return state["bid_files"][state["current_bid_index"]]


def applicable_rules_for_file(rule_set: RuleSet, _: DocumentRef) -> list:
    """批量审核版本中，相关文件默认一次接收全部最终规则。"""

    return list(rule_set.rules)
