"""任务错误和主 Agent 轻量审核 Decision。"""

from pydantic import Field

from app.models.common import FindingResult, RuleCategory, StrictModel


class TaskError(StrictModel):
    code: str
    stage: str
    message: str
    retryable: bool = False
    file_id: str | None = None
    claim_id: str | None = None


class RuleAuditSummary(StrictModel):
    reason: str = Field(min_length=2, max_length=200)
    tender_chunk_ids: list[str] = Field(default_factory=list)
    bid_chunk_ids: list[str] = Field(default_factory=list)
    evidence_chunk_ids: list[str] = Field(default_factory=list)


class RuleAuditDecision(StrictModel):
    rule_id: str
    result: FindingResult
    summary: RuleAuditSummary


class CategoryAuditDecision(StrictModel):
    category: RuleCategory
    summary: str = Field(min_length=2, max_length=500)
    rules: list[RuleAuditDecision] = Field(default_factory=list)


class AgentAuditDecision(StrictModel):
    summary: str = Field(min_length=2, max_length=800)
    categories: list[CategoryAuditDecision] = Field(default_factory=list)


BidAgentDecision = AgentAuditDecision
