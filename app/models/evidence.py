"""资料分类、Claim、候选证据和资料核验结果模型。"""

from pydantic import Field

from app.models.common import (
    ClassificationStatus,
    EvidenceReviewStatus,
    EvidenceType,
    StrictModel,
)
from app.models.documents import SourceRef


class EvidenceClassification(StrictModel):
    evidence_type: EvidenceType
    confidence: float = Field(ge=0, le=1)
    classification_status: ClassificationStatus
    reason: str


class Claim(StrictModel):
    claim_id: str
    task_id: str
    project_id: str
    bidder_id: str
    rule_id: str
    bid_file_id: str
    statement: str
    required_evidence_types: list[EvidenceType]
    source: SourceRef


class EvidenceCandidate(StrictModel):
    file_id: str
    chunk_id: str
    project_id: str
    bidder_id: str
    evidence_type: EvidenceType
    score: float
    summary: str
    source: SourceRef


class EvidenceReviewResult(StrictModel):
    evidence_result_id: str
    claim_id: str
    task_id: str
    project_id: str
    bidder_id: str
    bid_file_id: str
    rule_id: str
    status: EvidenceReviewStatus
    evidence_found: list[SourceRef] = Field(default_factory=list)
    missing_evidence: list[str] = Field(default_factory=list)
    consistency_issues: list[str] = Field(default_factory=list)
    manual_review_reason: str | None = None
    summary: str
