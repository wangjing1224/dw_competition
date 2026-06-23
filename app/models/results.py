"""单文件审核结果和任务级 AuditResult 根模型。"""

from typing import Literal

from pydantic import Field, model_validator

from app.models.common import (
    OverallConclusion,
    ProjectMatchStatus,
    StrictModel,
)
from app.models.documents import BidDocumentSummary, DocumentRef
from app.models.evidence import EvidenceReviewResult
from app.models.findings import Finding


class ProjectMatch(StrictModel):
    status: ProjectMatchStatus
    reason: str
    matched_fields: list[str] = Field(default_factory=list)
    conflicting_fields: list[str] = Field(default_factory=list)


class FileAuditResult(StrictModel):
    file: DocumentRef
    is_applicable: bool
    project_match: ProjectMatch
    document_summary: BidDocumentSummary | None = None
    exclusion_reason: str | None = None
    overall_conclusion: OverallConclusion
    findings: list[Finding] = Field(default_factory=list)
    evidence_results: list[EvidenceReviewResult] = Field(default_factory=list)
    suggestions: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_applicability(self) -> "FileAuditResult":
        if self.is_applicable:
            if self.overall_conclusion == OverallConclusion.NOT_APPLICABLE:
                raise ValueError("适用文件不能标记为 not_applicable")
        else:
            if not self.exclusion_reason:
                raise ValueError("未进入自动审核的文件必须说明原因")
            expected = (
                OverallConclusion.NOT_APPLICABLE
                if self.project_match.status == "not_matched"
                else OverallConclusion.MANUAL_REVIEW
            )
            if self.overall_conclusion != expected:
                raise ValueError("文件总体结论与路由状态不一致")
            if self.findings:
                raise ValueError("不适用文件不能包含普通审核 Finding")
        return self


class AuditResult(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    task_id: str
    project_id: str
    bidder_id: str
    file_results: list[FileAuditResult]

    @model_validator(mode="after")
    def validate_identity_and_coverage(self) -> "AuditResult":
        file_ids: set[str] = set()
        for file_result in self.file_results:
            file_id = file_result.file.file_id
            if file_id in file_ids:
                raise ValueError("AuditResult 中存在重复文件结果")
            file_ids.add(file_id)
            if file_result.file.project_id != self.project_id:
                raise ValueError("文件结果引用了其他项目")
            if file_result.file.bidder_id != self.bidder_id:
                raise ValueError("文件结果引用了其他投标方")
            if file_result.file.task_id != self.task_id:
                raise ValueError("文件结果引用了其他审核任务")

            for finding in file_result.findings:
                identities = (
                    finding.task_id,
                    finding.project_id,
                    finding.bidder_id,
                    finding.bid_file_id,
                )
                expected = (self.task_id, self.project_id, self.bidder_id, file_id)
                if identities != expected:
                    raise ValueError("Finding 归属与 AuditResult 不一致")
            for review in file_result.evidence_results:
                identities = (
                    review.task_id,
                    review.project_id,
                    review.bidder_id,
                    review.bid_file_id,
                )
                expected = (self.task_id, self.project_id, self.bidder_id, file_id)
                if identities != expected:
                    raise ValueError("EvidenceReviewResult 归属不一致")
        return self
