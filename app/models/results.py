"""单文件审核结果和任务级 AuditResult 根模型。"""

from typing import Literal

from pydantic import Field, model_validator

from app.models.common import (
    OverallConclusion,
    ProjectMatchStatus,
    StrictModel,
)
from app.models.documents import DocumentRef
from app.models.evidence import EvidenceLibrary
from app.models.findings import Finding
from app.models.rules import RuleReportItem


class ReadSource(StrictModel):
    """本次实际展示的来源快照，离开聊天历史仍能校验、导出。"""

    file_id: str
    file_name: str
    source_path: str
    data_role: str
    parse_version: str
    section: str | None = None
    page_numbers: list[int] = Field(default_factory=list)
    quote: str = ""


class ProjectMatch(StrictModel):
    status: ProjectMatchStatus
    reason: str
    matched_fields: list[str] = Field(default_factory=list)
    conflicting_fields: list[str] = Field(default_factory=list)


class FileAuditResult(StrictModel):
    file: DocumentRef
    is_applicable: bool
    project_match: ProjectMatch
    exclusion_reason: str | None = None
    overall_conclusion: OverallConclusion
    audit_summary: str | None = None
    category_summaries: dict[str, str] = Field(default_factory=dict)
    findings: list[Finding] = Field(default_factory=list)
    suggestions: list[str] = Field(default_factory=list)
    sources: dict[str, ReadSource] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_applicability(self) -> "FileAuditResult":
        if self.is_applicable:
            if self.project_match.status != "matched":
                raise ValueError("只有 matched 文件能进入审核")
            if self.overall_conclusion == OverallConclusion.NOT_APPLICABLE:
                raise ValueError("适用文件不能标记为 not_applicable")
        else:
            if self.project_match.status == "matched":
                raise ValueError("matched 文件不能标记为路由排除")
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
    schema_version: Literal["2.0"] = "2.0"
    task_id: str
    project_id: str
    bidder_id: str
    rules: list[RuleReportItem] = Field(default_factory=list)
    evidence_library: EvidenceLibrary
    rule_source_files: list[dict[str, str]] = Field(default_factory=list)
    file_results: list[FileAuditResult]
    file_errors: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_identity_and_coverage(self) -> "AuditResult":
        rule_ids = [rule.rule_id for rule in self.rules]
        if len(rule_ids) != len(set(rule_ids)):
            raise ValueError("AuditResult 规则快照存在重复 rule_id")
        if self.evidence_library.bidder_id != self.bidder_id:
            raise ValueError("资料库与投标方不一致")
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
            if file_result.is_applicable:
                covered = [item.rule_id for item in file_result.findings]
                if len(covered) != len(known_rules) or set(covered) != known_rules:
                    raise ValueError("已审核文件必须每条规则恰好覆盖一次")
            for finding in file_result.findings:
                for field, role in (
                    ("tender_chunk_ids", "rule_source"),
                    ("bid_chunk_ids", "bid_document"),
                    ("evidence_chunk_ids", "evidence_material"),
                    ("image_paths", None),
                ):
                    for key in getattr(finding, field):
                        source = file_result.sources.get(key)
                        if source is None or (role and source.data_role != role):
                            raise ValueError("报告引用缺少来源快照或类型错误")
                        allowed = {
                            "rule_source": {
                                s["file_id"] for s in self.rule_source_files
                            },
                            "evidence_material": {
                                s.file_id for s in self.evidence_library.source_files
                            },
                            "bid_document": {file_id},
                        }
                        if source.file_id not in allowed.get(source.data_role, set()):
                            raise ValueError("报告来源超出当前文件授权范围")
        if file_ids & self.file_errors.keys():
            raise ValueError("同一文件不能同时标记成功和执行失败")
        return self
