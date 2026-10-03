"""仅绑定当前稿的文本编辑工具，报告结构不进入编辑参数。"""

from typing import Annotated, Literal

from langchain_core.tools import InjectedToolCallId, StructuredTool
from pydantic import Field, model_validator

from app.models.common import StrictModel
from app.prompts.loader import load_tool_description


class ReportEdit(StrictModel):
    old_text: str = Field(
        min_length=1,
        description="当前稿中恰好出现一次的完整原文；必要时带相邻标题消歧。",
    )
    new_text: str = Field(
        description="替换后的文本，空字符串表示删除。保存后返回校验反馈，定稿须校验通过。"
    )


class ReportInput(StrictModel):
    action: Literal["read", "write", "edit"] = Field(
        description="read 读取当前稿；write 完整覆盖；edit 按顺序精确替换。"
    )
    content: str | None = Field(
        description="write 填完整 Markdown；其他动作显式 null。"
    )
    edits: list[ReportEdit] | None = Field(
        description="edit 填非空精确替换列表；其他动作显式 null。"
    )
    tool_call_id: Annotated[str, InjectedToolCallId]

    @model_validator(mode="after")
    def validate_action(self):
        if self.action == "write":
            valid = bool(self.content and self.content.strip()) and self.edits is None
        elif self.action == "edit":
            valid = self.content is None and bool(self.edits)
        else:
            valid = self.content is None and self.edits is None
        if not valid:
            raise ValueError("read/write/edit 的 content、edits 组合不合法")
        return self


def report_tool(report):
    async def report_document(
        action, content, edits, tool_call_id: Annotated[str, InjectedToolCallId]
    ):
        return await report.apply(action, content, edits, tool_call_id)

    return StructuredTool.from_function(
        coroutine=report_document,
        name="report_document",
        args_schema=ReportInput,
        description=load_tool_description("report_document"),
    )
