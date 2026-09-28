"""任务错误及 Markdown 解析后的内部结果对象；不再作为模型输出 Schema。"""

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
    reason: str = Field(min_length=1)
    tender_chunk_ids: list[str] = Field(default_factory=list)
    bid_chunk_ids: list[str] = Field(default_factory=list)
    evidence_chunk_ids: list[str] = Field(default_factory=list)
    image_paths: list[str] = Field(default_factory=list)


class RuleAuditDecision(StrictModel):
    rule_id: str
    result: FindingResult
    summary: RuleAuditSummary


class CategoryAuditDecision(StrictModel):
    category: RuleCategory
    summary: str = Field(min_length=1)
    rules: list[RuleAuditDecision] = Field(default_factory=list)


class AgentAuditDecision(StrictModel):
    report_title: str = Field(min_length=1)
    summary: str = Field(min_length=1)
    categories: list[CategoryAuditDecision] = Field(default_factory=list)


BidAgentDecision = AgentAuditDecision
