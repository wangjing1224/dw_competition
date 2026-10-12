"""仅绑定当前稿的文本编辑工具，报告结构不进入编辑参数。"""

from typing import Annotated, Literal

from langchain_core.tools import InjectedToolCallId, StructuredTool
from pydantic import Field, create_model, model_validator

from app.models.common import StrictModel
from app.prompts.loader import load_tool_description


class ReportSavePolicy:
    """主审草稿进度跟踪；待保存仅作提醒，不限制工具或阻止会话退出。"""

    def __init__(self, report, checkpoint, *, pending=None, preflight=None):
        self.report, self.checkpoint = report, checkpoint
        self.pending = list(pending or [])
        self.preflight = preflight
        self.executed = []
        self.step = 0
        self.known = {r.rule_id for r in report.rule_set.rules}
        if any(not set(item["rule_ids"]) <= self.known for item in self.pending):
            raise ValueError("待保存状态含非冻结规则")
        report.after_save = self.saved
        report.pending_status = self.status

    def status(self):
        ids = {key for item in self.pending for key in item["rule_ids"]}
        return "待保存规则：" + ("、".join(r.rule_id for r in self.report.rule_set.rules if r.rule_id in ids) or "无")

    def select_tools(self, tools):
        return tools

    def begin_batch(self, step):
        self.step, self.executed = step, []

    def after_batch(self):
        self.pending.extend(self.executed)
        self.executed = []
        # 落盘失败不清除内存待保存，也不让会话继续声明完成。
        self.checkpoint()

    def check_exit(self):
        # 保留通用会话钩子；能否定稿由报告的实际完整性与来源校验决定。
        # pending是保存提醒，不再作为独立的退出门禁。
        return None

    def saved(self):
        from app.reports.report_parser_v2 import draft_rule_states

        states = draft_rule_states(self.report.read(), self.report.rule_set, self.report.ledger)
        previous = self.pending
        remaining = []
        for item in previous:
            later = self.report.batch_key > item["batch_index"] or (
                self.report.batch_key == item["batch_index"] and self.step > item["tool_round"]
            )
            ids = [key for key in item["rule_ids"] if not later or states[key][0] is None]
            if ids:
                remaining.append({**item, "rule_ids": ids})
        self.pending = remaining
        try:
            self.checkpoint()
        except BaseException:
            self.pending = previous
            raise

    def adapt_tools(self, tools):
        return [self._adapt(tool) if tool.name != "report_document" else tool for tool in tools]

    def _adapt(self, tool):
        delegate = tool.name == "request_evidence_review"
        fields = {} if delegate else {
            "rule_ids": (list[str], Field(min_length=1, description="本次调用关联的冻结规则ID，非空且不重复；只用于后续保存进度，不是来源引用。")),
        }
        if "tool_call_id" not in tool.args_schema.model_fields:
            fields["tool_call_id"] = (Annotated[str, InjectedToolCallId], ...)
        schema = create_model(f"Main{tool.args_schema.__name__}", __base__=tool.args_schema, **fields)

        async def invoke(**kwargs):
            call_id = kwargs.pop("tool_call_id")
            if delegate:
                ids = list(dict.fromkeys(key for check in kwargs["checks"] for key in check.rule_ids))
            else:
                ids = kwargs.pop("rule_ids")
                if len(ids) != len(set(ids)):
                    raise ValueError("rule_ids不允许重复")
            if not ids or not set(ids) <= self.known:
                raise ValueError("rule_ids必须关联当前冻结规则")
            if self.preflight:
                self.preflight(tool.name, kwargs)
            # 只有通过适配层参数/关联预检、将进入原工具的调用才登记。
            self.executed.append({"batch_index": self.report.batch_key, "tool_round": self.step,
                                  "tool_call_id": call_id, "rule_ids": ids})
            def plain(value):
                if hasattr(value, "model_dump"):
                    return value.model_dump()
                if isinstance(value, list):
                    return [plain(item) for item in value]
                return value
            return await tool.ainvoke({"type": "tool_call", "name": tool.name, "id": call_id,
                                       "args": {k: plain(v) for k, v in kwargs.items()}})

        return StructuredTool.from_function(
            coroutine=invoke, name=tool.name, args_schema=schema,
            description=tool.description + ("\n主审调用使用checks已有rule_ids关联后续草稿进度。" if delegate else "\n主审专用：另传rule_ids关联后续草稿进度；原工具权限不变。"),
        )


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
