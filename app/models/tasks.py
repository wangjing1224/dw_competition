"""任务错误和主 Agent 单轮 Decision 判别联合。"""

from typing import Annotated, Literal

from pydantic import Field, TypeAdapter, model_validator

from app.models.evidence import EvidenceReviewResult
from app.models.findings import Finding
from app.models.results import FileAuditResult, ProjectMatch
from app.models.common import StrictModel


class TaskError(StrictModel):
    code: str
    stage: str
    message: str
    retryable: bool = False
    file_id: str | None = None
    claim_id: str | None = None


class RecordFindingDecision(StrictModel):
    action: Literal["record_finding"]
    reason: str
    finding: Finding
    project_match: None = None
    is_applicable: None = None
    exclusion_reason: None = None
    file_result: None = None
    evidence_results: list[EvidenceReviewResult] = Field(default_factory=list)


class RecordProjectMatchDecision(StrictModel):
    action: Literal["record_project_match"]
    reason: str
    project_match: ProjectMatch
    is_applicable: bool
    exclusion_reason: str | None = None
    finding: None = None
    file_result: None = None
    evidence_results: list[EvidenceReviewResult] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_applicability(self) -> "RecordProjectMatchDecision":
        if self.project_match.status == "not_matched":
            if self.is_applicable or not self.exclusion_reason:
                raise ValueError("不匹配文件必须排除并说明原因")
        elif not self.is_applicable:
            raise ValueError("匹配或不确定文件应继续审核")
        return self


class RecordFileResultDecision(StrictModel):
    action: Literal["record_file_result"]
    reason: str
    project_match: None = None
    is_applicable: None = None
    exclusion_reason: None = None
    finding: None = None
    file_result: FileAuditResult
    evidence_results: list[EvidenceReviewResult] = Field(default_factory=list)


BidAgentDecision = Annotated[
    RecordProjectMatchDecision | RecordFindingDecision | RecordFileResultDecision,
    Field(discriminator="action"),
]
BidAgentDecisionAdapter = TypeAdapter(BidAgentDecision)
