"""规则级审核结论 Finding 模型。"""

from pydantic import Field, model_validator

from app.models.common import (
    FindingResult,
    ResponseStatus,
    RuleCategory,
    Severity,
    StrictModel,
)
from app.models.documents import SourceRef


class Finding(StrictModel):
    finding_id: str
    task_id: str
    project_id: str
    bidder_id: str
    bid_file_id: str
    rule_id: str
    response_status: ResponseStatus
    category: RuleCategory
    severity: Severity
    result: FindingResult
    title: str
    description: str
    rule_source: SourceRef
    bid_source: SourceRef | None = None
    evidence_result_id: str | None = None
    suggestion: str | None = None
    confidence: float = Field(ge=0, le=1)

    @model_validator(mode="after")
    def validate_bid_source(self) -> "Finding":
        if self.response_status == ResponseStatus.FOUND and self.bid_source is None:
            raise ValueError("找到响应时必须提供投标响应来源")
        if (
            self.response_status == ResponseStatus.NOT_FOUND
            and self.bid_source is not None
        ):
            raise ValueError("未找到响应时 bid_source 应为空")
        return self
