"""单一规则库、文件级来源和 Agent 增量协议；不兼容旧规则产物。"""

from typing import Annotated, Literal

from pydantic import Field, StringConstraints, model_validator

from app.models.common import (
    RuleCategory,
    RulePriority,
    RuleProvenance,
    StrictModel,
    VerificationLevel,
)
from app.models.documents import DocumentRef

NonBlank = Annotated[str, StringConstraints(pattern=r"\S")]


class RuleSourceFile(StrictModel):
    """从上传记录投影出的只读文件快照，不包含业务归属或切片。"""

    file_id: NonBlank
    file_name: NonBlank
    source_path: NonBlank

    @classmethod
    def from_document(cls, document: DocumentRef) -> "RuleSourceFile":
        return cls(**document.model_dump(include=set(cls.model_fields)))


class RuleItem(StrictModel):
    rule_id: str
    provenance: RuleProvenance
    category: RuleCategory
    requirement: NonBlank
    priority: RulePriority
    verification: VerificationLevel
    reason: str
    sources: list[RuleSourceFile] = Field(min_length=1)

    @model_validator(mode="after")
    def unique_sources(self):
        ids = [source.file_id for source in self.sources]
        if len(ids) != len(set(ids)):
            raise ValueError("规则来源 file_id 重复")
        return self


class RuleModelView(StrictModel):
    """供审核消费端使用的投影；provenance 不参与模型判断。"""

    rule_id: str
    category: RuleCategory
    requirement: str
    priority: RulePriority
    verification: VerificationLevel
    reason: str
    source_file_ids: list[str]


class RuleReportItem(RuleModelView):
    """报告中的规则快照，按 rule_id 拼接审核结论。"""


class RuleDeltaOperation(StrictModel):
    action: Literal["add", "merge", "replace"] = Field(
        description="新增、追加增量、完整覆盖。"
    )
    rule_id: str | None = Field(
        default=None, description="merge/replace 引用已有 ID；add 不填写。"
    )
    merged_rule_ids: list[NonBlank] = Field(
        default_factory=list,
        description="仅 merge：有当前文件依据时明确吸收的其他规则 ID。",
    )
    category: RuleCategory | None = Field(
        default=None, description="add/replace 必填；merge 可沿用。"
    )
    requirement: NonBlank | None = Field(
        default=None,
        description="add/replace 填完整要求；merge 只填新增文本，纯补来源可省略。",
    )
    priority: RulePriority | None = Field(
        default=None, description="add/replace 必填；merge 不降低原重要程度。"
    )
    verification: VerificationLevel | None = Field(
        default=None, description="bid_only 或 evidence_check；add/replace 必填。"
    )
    source_file_ids: list[NonBlank] | None = Field(
        default=None,
        description="仅 replace 必填完整非空来源文件 ID 列表；add/merge 不填写。",
    )
    reason: str = Field(
        description="更新后整条规则的完整依据说明，明确条件来自哪些文件；始终覆盖，可为空字符串。"
    )

    @model_validator(mode="after")
    def validate_action(self):
        if self.action in {"add", "replace"}:
            if any(
                getattr(self, key) is None
                for key in ("category", "requirement", "priority", "verification")
            ):
                raise ValueError(
                    "add/replace 必须提供 category/requirement/priority/verification"
                )
            if "merged_rule_ids" in self.model_fields_set:
                raise ValueError("merged_rule_ids 仅用于 merge")
        if self.action == "add":
            if "rule_id" in self.model_fields_set:
                raise ValueError("add 的 rule_id 由系统生成，请勿填写")
        elif not self.rule_id:
            raise ValueError("merge/replace 必须引用已有 rule_id")
        if self.action == "replace":
            if not self.source_file_ids:
                raise ValueError("replace 必须提供完整非空 source_file_ids")
            if len(set(self.source_file_ids)) != len(self.source_file_ids):
                raise ValueError("source_file_ids 不能重复")
        elif "source_file_ids" in self.model_fields_set:
            raise ValueError("add/merge 来源由系统维护，请勿填写 source_file_ids")
        if self.rule_id in self.merged_rule_ids or len(
            set(self.merged_rule_ids)
        ) != len(self.merged_rule_ids):
            raise ValueError("被吸收 ID 不能重复或包含目标自身")
        return self


class SubmitRuleDeltaInput(StrictModel):
    operations: list[RuleDeltaOperation] | None = Field(
        min_length=1, description="非空操作列表；没有规则操作时显式填 null。"
    )
    project_profile: NonBlank | None = Field(
        description="完整新项目简介；不修改时显式填 null。"
    )
    return_view: Literal["changed", "all"] | None = Field(
        description=(
            "仅有真实写入时选 changed（本批变更）或 all（保存后的全库）。"
            "只读时必须填 null，不要填 all；只读完整参数："
            '{"operations":null,"project_profile":null,"return_view":null}。'
        )
    )

    @model_validator(mode="after")
    def validate_mode(self):
        writing = self.operations is not None or self.project_profile is not None
        if writing != (self.return_view is not None):
            raise ValueError("写入须指定 changed/all；只读须三个参数均为 null")
        return self


class RuleSet(StrictModel):
    project_id: str
    project_profile: str = ""
    rules: list[RuleItem] = Field(default_factory=list)
    source_files: list[RuleSourceFile] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_sources(self):
        files = {source.file_id: source for source in self.source_files}
        if len(files) != len(self.source_files):
            raise ValueError("source_files 存在重复 file_id")
        if len({rule.rule_id for rule in self.rules}) != len(self.rules):
            raise ValueError("存在重复 rule_id")
        for rule in self.rules:
            for source in rule.sources:
                if files.get(source.file_id) != source:
                    raise ValueError("规则来源不在本项目文件快照中")
        return self


class RuleCommitRecord(StrictModel):
    tool_call_id: str
    before: list[RuleItem]
    after: list[RuleItem]
    absorbed_ids: dict[str, str] = Field(default_factory=dict)


class RuleExtractionProgress(StrictModel):
    project_id: str
    document_index: int = Field(ge=1)
    document_count: int = Field(ge=1)
    current_file_id: str
    completed_file_ids: list[str] = Field(default_factory=list)
    parser_versions: dict[str, str] = Field(default_factory=dict)
    model_calls: int = Field(ge=0)
    next_unit_index: int = Field(default=0, ge=0)
    batch_start: int = Field(default=0, ge=0)
    batch_end: int = Field(default=0, ge=0)
    batch_index: int = Field(default=0, ge=0)
    file_model_calls: int = Field(default=0, ge=0)
    compaction_calls: int = Field(default=0, ge=0)
    compaction_records: list[dict] = Field(default_factory=list)
    last_budget: dict = Field(default_factory=dict)
    rule_set: RuleSet
    commits: list[RuleCommitRecord] = Field(default_factory=list)
    error: str | None = None


def present_rule_for_model(rule: RuleItem) -> RuleModelView:
    return RuleModelView(
        **rule.model_dump(exclude={"sources", "provenance"}),
        source_file_ids=[source.file_id for source in rule.sources],
    )


def present_rules_for_model(rule_set: RuleSet) -> list[RuleModelView]:
    return [present_rule_for_model(rule) for rule in rule_set.rules]
