"""人工编辑只接受明确字段；不接收 Agent 的状态或整个规则库。"""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from app.models.common import RuleCategory, RulePriority, VerificationLevel

Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class EditData(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ProfileData(EditData):
    project_profile: str


class AddRuleData(EditData):
    category: RuleCategory
    requirement: Text
    priority: RulePriority
    verification: VerificationLevel
    reason: str
    source_file_ids: list[Text] = Field(min_length=1)


class UpdateRuleData(AddRuleData):
    rule_id: Text


class ProfileEdit(EditData):
    action: Literal["update_profile"]
    data: ProfileData


class AddRule(EditData):
    action: Literal["add_rule"]
    data: AddRuleData


class UpdateRule(EditData):
    action: Literal["update_rule"]
    data: UpdateRuleData


ProjectEdit = Annotated[
    ProfileEdit | AddRule | UpdateRule, Field(discriminator="action")
]
