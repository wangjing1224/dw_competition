"""完整 RuleSet、模型投影视图和规则增量操作模型。"""

from typing import Literal

from pydantic import Field, model_validator

from app.models.common import (
    EvidenceType,
    RuleCategory,
    RulePriority,
    RuleStatus,
    StrictModel,
    VerificationLevel,
)
from app.models.documents import DocumentRef, SourceRef


class ProjectProfile(StrictModel):
    project_name: str | None = None
    buyer: str | None = None
    material_category: str | None = None
    bid_deadline: str | None = None
    bid_validity_days: int | None = Field(default=None, ge=0)


class RuleItem(StrictModel):
    """持久化规则。

    模型调用只读取 ``RuleModelView``；来源字段仅供代码做溯源和报告拼接。
    """

    rule_id: str
    status: RuleStatus = RuleStatus.ACTIVE
    category: RuleCategory
    priority: RulePriority = RulePriority.NORMAL
    verification: VerificationLevel = VerificationLevel.BID_ONLY
    requirement: str = Field(min_length=8, max_length=10000)
    evidence_types: list[EvidenceType] = Field(default_factory=list)
    conflict_note: str | None = None
    source: SourceRef
    supporting_sources: list[SourceRef] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_rule(self) -> "RuleItem":
        if (
            self.verification in {
                VerificationLevel.EVIDENCE_CHECK,
                VerificationLevel.CROSS_CHECK,
            }
            and not self.evidence_types
            and self.verification == VerificationLevel.EVIDENCE_CHECK
        ):
            raise ValueError("资料核验规则必须声明 evidence_types")
        if self.status == RuleStatus.RULE_CONFLICT and not self.conflict_note:
            raise ValueError("冲突规则必须说明冲突原因")
        return self


class RuleModelView(StrictModel):
    """临时发送给 LLM 的规则投影，不形成第二套规则库。"""

    rule_id: str
    category: RuleCategory
    requirement: str = Field(min_length=8, max_length=10000)
    priority: RulePriority
    verification: VerificationLevel
    evidence_types: list[EvidenceType] = Field(default_factory=list)


class RuleReportItem(RuleModelView):
    """最终 JSON 顶层保存的规则快照，供 Word 按 rule_id 展示。"""


class RuleDeltaOperation(StrictModel):
    action: Literal["add", "merge"]
    rule_id: str | None = None
    merged_rule_ids: list[str] = Field(default_factory=list)
    category: RuleCategory | None = None
    requirement: str | None = Field(default=None, min_length=8, max_length=2000)
    priority: RulePriority | None = None
    verification: VerificationLevel | None = None
    evidence_types: list[EvidenceType] = Field(default_factory=list)
    reason: str = Field(min_length=2, max_length=300)

    @model_validator(mode="after")
    def validate_action_fields(self) -> "RuleDeltaOperation":
        if self.action == "add":
            required = (
                self.category,
                self.requirement,
                self.priority,
                self.verification,
            )
            if any(value is None for value in required):
                raise ValueError("add 必须提供 category/requirement/priority/verification")
        elif not self.rule_id:
            raise ValueError("merge 必须提供 rule_id")
        if self.rule_id and self.rule_id in self.merged_rule_ids:
            raise ValueError("merged_rule_ids 不能包含保留目标 rule_id")
        return self


class RuleDeltaResult(StrictModel):
    project_profile: ProjectProfile = Field(default_factory=ProjectProfile)
    operations: list[RuleDeltaOperation] = Field(default_factory=list)


class RuleSet(StrictModel):
    project_id: str
    project_profile: ProjectProfile = Field(default_factory=ProjectProfile)
    rules: list[RuleItem] = Field(default_factory=list)
    source_files: list[DocumentRef] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_sources(self) -> "RuleSet":
        for source_file in self.source_files:
            if source_file.project_id != self.project_id:
                raise ValueError("RuleSet 不能引用其他项目文件")
        return self


class RuleExtractionProgress(StrictModel):
    """最近一次成功批次的规则快照，失败时保留用于排查。"""

    project_id: str
    document_index: int = Field(ge=1)
    document_count: int = Field(ge=1)
    batch_index: int = Field(ge=1)
    batch_count: int = Field(ge=1)
    source_file: str
    rule_count: int = Field(ge=0)
    rule_set: RuleSet


def present_rule_for_model(rule: RuleItem) -> RuleModelView:
    return RuleModelView(
        rule_id=rule.rule_id,
        category=rule.category,
        requirement=rule.requirement,
        priority=rule.priority,
        verification=rule.verification,
        evidence_types=rule.evidence_types,
    )


def present_rules_for_model(rule_set: RuleSet) -> list[RuleModelView]:
    return [present_rule_for_model(rule) for rule in rule_set.rules]
