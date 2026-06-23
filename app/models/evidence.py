"""资料分类、Claim、候选证据和资料核验结果模型。"""

from pydantic import Field

from app.models.common import (
    ClassificationStatus,
    EvidenceReviewStatus,
    EvidenceType,
    RuleCategory,
    StrictModel,
)
from app.models.documents import SourceRef


class EvidenceCatalogProfile(StrictModel):
    rule_categories: list[RuleCategory] = Field(default_factory=list)
    summary: str
    verifiable_topics: list[str] = Field(default_factory=list)


class EvidenceRetrievalProfile(StrictModel):
    document_title: str
    entities: list[str] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)
    searchable_facts: list[str] = Field(default_factory=list)


class EvidenceClassification(StrictModel):
    evidence_type: EvidenceType
    confidence: float = Field(ge=0, le=1)
    classification_status: ClassificationStatus
    reason: str
    catalog_profile: EvidenceCatalogProfile
    retrieval_profile: EvidenceRetrievalProfile


class EvidenceCatalogEntry(StrictModel):
    available: bool
    count: int = Field(ge=0)
    summary: str
    evidence_types: list[EvidenceType] = Field(default_factory=list)
    verifiable_topics: list[str] = Field(default_factory=list)


class Claim(StrictModel):
    claim_id: str
    task_id: str
    project_id: str
    bidder_id: str
    rule_id: str
    bid_file_id: str
    rule_category: RuleCategory
    statement: str
    evidence_group: RuleCategory
    required_evidence_types: list[EvidenceType]
    retrieval_query: str
    keywords: list[str] = Field(default_factory=list)
    verification_points: list[str] = Field(default_factory=list)
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
