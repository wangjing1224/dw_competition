"""单文件审核结果和任务级 AuditResult 根模型。"""

from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator

from app.models.common import (
    OverallConclusion,
    ProjectMatchStatus,
    StrictModel,
)
from app.models.documents import DocumentRef
from app.models.evidence import EvidenceLibrary
from app.models.findings import Finding, ReportHighlight
from app.models.rules import RuleReportItem, RuleSet, present_rule_for_model


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


class ReportContext(StrictModel):
    task_name: str | None = None
    rule_library_name: str | None = None
    evidence_library_name: str | None = None
    finalized_at: datetime | None = None


class FileAuditResult(StrictModel):
    file: DocumentRef
    # 从模型成功保存的 Markdown 一级标题提取；旧结果可没有此字段。
    report_title: str | None = Field(default=None, min_length=1)
    report_format_version: Literal[1, 2, 3] = 1
    report_context: ReportContext | None = None
    highlights: list[ReportHighlight] = Field(default_factory=list)
    is_applicable: bool
    project_match: ProjectMatch
    exclusion_reason: str | None = None
    overall_conclusion: OverallConclusion
    audit_summary: str | None = None
    category_summaries: dict[str, str] = Field(default_factory=dict)
    findings: list[Finding] = Field(default_factory=list)
    suggestions: list[str] = Field(default_factory=list)
    sources: dict[str, ReadSource] = Field(default_factory=dict)

    @property
    def display_title(self) -> str:
        if self.report_title and self.report_title.strip():
            return self.report_title.strip()
        # 路由排除不调用审核 Agent；旧 JSON 同样不能额外请求模型补标题。
        name = (
            Path(self.file.file_name).stem.replace("\r", " ").replace("\n", " ").strip()
        )
        suffix = "审核报告" if self.is_applicable else "项目适用性说明"
        return f"{name or self.file.file_id}{suffix}"

    @model_validator(mode="after")
    def validate_applicability(self) -> "FileAuditResult":
        if (
            self.report_format_version in (2, 3)
            and self.is_applicable
            and any(
                not (f.observation or "").strip() or not f.summary.strip()
                for f in self.findings
            )
        ):
            raise ValueError("新版规则结果必须有实际发现和判断说明")
        findings = {f.rule_id: f for f in self.findings}
        for highlight in self.highlights:
            if (
                not highlight.title.strip()
                or not highlight.summary.strip()
                or len(set(highlight.rule_ids)) != len(highlight.rule_ids)
                or any(key not in findings for key in highlight.rule_ids)
            ):
                raise ValueError("重点问题的内容或关联规则非法")
            if all(findings[key].result == "compliant" for key in highlight.rule_ids):
                raise ValueError("重点问题须关联至少一项非符合结论")
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
            if self.findings or self.highlights:
                raise ValueError("不适用文件不能包含普通审核 Finding")
        return self


class AuditBatchFile(StrictModel):
    file_id: str
    project_id: str
    bidder_id: str
    status: Literal["completed", "failed"]
    result: FileAuditResult | None = None
    error: dict[str, str] | None = None
    export_error: dict[str, str] | None = None


class AuditBatchResult(StrictModel):
    """管理批次不再假设一个项目；库快照去重，业务校验逐文件复用。"""

    schema_version: Literal["3.0"] = "3.0"
    task_id: str
    rule_libraries: dict[str, RuleSet]
    evidence_libraries: dict[str, EvidenceLibrary]
    files: list[AuditBatchFile]

    @model_validator(mode="after")
    def validate_files(self):
        if len({f.file_id for f in self.files}) != len(self.files):
            raise ValueError("批次存在重复文件")
        for item in self.files:
            if (
                item.project_id not in self.rule_libraries
                or item.bidder_id not in self.evidence_libraries
            ):
                raise ValueError("文件绑定缺少对应的库快照")
            if (item.status == "completed") != (item.result is not None):
                raise ValueError("终态与正式结果不一致")
            if item.result:
                rules = self.rule_libraries[item.project_id]
                if (
                    rules.project_id != item.project_id
                    or item.result.file.file_id != item.file_id
                ):
                    raise ValueError("文件或规则库身份不一致")
                AuditResult(
                    task_id=self.task_id,
                    project_id=item.project_id,
                    bidder_id=item.bidder_id,
                    rules=[
                        RuleReportItem.model_validate(
                            present_rule_for_model(r).model_dump()
                        )
                        for r in rules.rules
                    ],
                    evidence_library=self.evidence_libraries[item.bidder_id],
                    rule_source_files=[f.model_dump() for f in rules.source_files],
                    file_results=[item.result],
                )
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
