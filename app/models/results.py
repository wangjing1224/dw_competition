"""单文件审核结果和任务级 AuditResult 根模型。"""

from typing import Literal

from pydantic import Field, model_validator

from app.models.common import (
    OverallConclusion,
    ProjectMatchStatus,
    StrictModel,
)
from app.models.documents import BidDocumentSummary, DocumentRef
from app.models.evidence import EvidenceClassificationRecord
from app.models.findings import Finding
from app.models.rules import RuleReportItem


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
    audit_summary: str | None = None
    category_summaries: dict[str, str] = Field(default_factory=dict)
    findings: list[Finding] = Field(default_factory=list)
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
    rules: list[RuleReportItem] = Field(default_factory=list)
    evidence_classifications: list[EvidenceClassificationRecord] = Field(
        default_factory=list
    )
    file_results: list[FileAuditResult]

    @model_validator(mode="after")
    def validate_identity_and_coverage(self) -> "AuditResult":
        rule_ids = [rule.rule_id for rule in self.rules]
        if len(rule_ids) != len(set(rule_ids)):
            raise ValueError("AuditResult 规则快照存在重复 rule_id")
        classification_ids = [
            item.file_id for item in self.evidence_classifications
        ]
        if len(classification_ids) != len(set(classification_ids)):
            raise ValueError("AuditResult 资料分类存在重复 file_id")
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

            known_rules = set(rule_ids)
            if any(item.rule_id not in known_rules for item in file_result.findings):
                raise ValueError("Finding 引用了 AuditResult 中不存在的规则")
        return self
