"""RuleSet、规则作用域和审核规则模型。"""

from typing import Any

from pydantic import Field, model_validator

from app.models.common import (
    EvidenceType,
    RuleCategory,
    RulePriority,
    RuleStatus,
    Severity,
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


class RuleScope(StrictModel):
    package_no: str | None = None
    material_name: str | None = None


class RuleCheckItem(StrictModel):
    """复合规则中的一个可独立判断检查项。"""

    item_key: str
    requirement: str
    target_field: str | None = None
    operator: str | None = None
    expected_value: Any | None = None


class RuleItem(StrictModel):
    rule_id: str
    status: RuleStatus
    category: RuleCategory
    priority: RulePriority = RulePriority.NORMAL
    verification_level: VerificationLevel = VerificationLevel.BID_ONLY
    name: str
    requirement: str
    check_type: str
    target_field: str | None = None
    operator: str | None = None
    expected_value: Any | None = None
    check_items: list[RuleCheckItem] = Field(default_factory=list)
    scope: RuleScope = Field(default_factory=RuleScope)
    severity: Severity
    evidence_required: bool = False
    required_evidence_types: list[EvidenceType] = Field(default_factory=list)
    conflict_note: str | None = None
    source: SourceRef
    supporting_sources: list[SourceRef] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_evidence_types(self) -> "RuleItem":
        if self.evidence_required and not self.required_evidence_types:
            raise ValueError("需要资料核验的规则必须声明证据类型")
        if self.status == RuleStatus.RULE_CONFLICT and not self.conflict_note:
            raise ValueError("冲突规则必须说明冲突原因")
        if not self.check_items:
            object.__setattr__(
                self,
                "check_items",
                [
                    RuleCheckItem(
                        item_key=self.target_field or self.name,
                        requirement=self.requirement,
                        target_field=self.target_field,
                        operator=self.operator,
                        expected_value=self.expected_value,
                    )
                ],
            )
        return self


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
