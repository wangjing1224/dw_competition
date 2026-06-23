"""任务错误和主 Agent 批量审核 Decision。"""

from typing import Literal

from pydantic import Field

from app.models.evidence import EvidenceReviewResult
from app.models.findings import Finding
from app.models.common import StrictModel


class TaskError(StrictModel):
    code: str
    stage: str
    message: str
    retryable: bool = False
    file_id: str | None = None
    claim_id: str | None = None


class BatchAuditDecision(StrictModel):
    action: Literal["record_batch"]
    reason: str
    file_id: str
    summary: str
    findings: list[Finding]
    evidence_results: list[EvidenceReviewResult] = Field(default_factory=list)


BidAgentDecision = BatchAuditDecision
