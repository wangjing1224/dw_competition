"""实际图调用隔离、成功草稿和失败保留。"""

import asyncio
import json

import pytest
from langchain_core.messages import AIMessage

from app.graph.audit_task_graph import LangGraphAuditExecutor
from tests.helpers.audit import (
    audit_context,
    filled_report,
    model_bundle,
    tool_response,
)


class ReportModel:
    def __init__(self, rules, fail_file=None):
        self.rules, self.fail_file = rules, fail_file
        self.calls = []
        self.active = self.peak = 0

    def bind_tools(self, tools):
        return self

    async def ainvoke(self, messages, **kwargs):
        self.calls.append(messages)
        self.active += 1
        self.peak = max(self.peak, self.active)
        await asyncio.sleep(0.01)
        self.active -= 1
        human = messages[1].content
        if self.fail_file and self.fail_file in human:
            raise RuntimeError("模型服务不可用")
        if any(m.type == "tool" for m in messages):
            return AIMessage(content="处理完毕")
        return tool_response(
            "report_document",
            {"action": "write", "content": filled_report(self.rules), "edits": None},
        )


@pytest.mark.asyncio
async def test_each_file_uses_independent_messages_and_report(
    monkeypatch, temp_settings
):
    monkeypatch.setattr("app.graph.audit_task_graph.verify_indexes", lambda *args: {})
    context = audit_context(2)
    model = ReportModel(context.rule_set)
    result = await LangGraphAuditExecutor(temp_settings, model_bundle(model)).run(
        context
    )
    assert not result.file_errors
    assert [r.file.file_id for r in result.file_results] == ["bid-1", "bid-2"]
    assert len(model.calls) == 4
    assert model.peak == 2
    for messages in model.calls:
        assert ("bid-1.md" in messages[1].content) != (
            "bid-2.md" in messages[1].content
        )
        assert "资料1简洁目录" in messages[1].content
        assert "资料1详细事实" not in messages[1].content
        assert "AgentAuditDecision" not in messages[0].content
    for file_id in context.file_order:
        root = temp_settings.storage.data_root / "outputs/audit-1"
        assert (root / f"{file_id}_result.json").exists()
        state = json.loads((root / f"drafts/{file_id}.state.json").read_text())
        assert state["next_unit_index"] == state["total_units"] == 1
        assert state["stats"]["model_calls"] == 2


@pytest.mark.asyncio
async def test_failed_file_preserves_draft_and_other_file(monkeypatch, temp_settings):
    monkeypatch.setattr("app.graph.audit_task_graph.verify_indexes", lambda *args: {})
    context = audit_context(2)
    result = await LangGraphAuditExecutor(
        temp_settings, model_bundle(ReportModel(context.rule_set, "bid-1.md"))
    ).run(context)
    assert list(result.file_errors) == ["bid-1"]
    assert "模型服务不可用" in result.file_errors["bid-1"]
    assert [r.file.file_id for r in result.file_results] == ["bid-2"]
    draft = temp_settings.storage.data_root / "outputs/audit-1/drafts/bid-1.md"
    assert "待填写。" in draft.read_text()


@pytest.mark.asyncio
async def test_completion_without_saved_report_is_failure(monkeypatch, temp_settings):
    from tests.helpers.audit import SequenceModel

    monkeypatch.setattr("app.graph.audit_task_graph.verify_indexes", lambda *args: {})
    result = await LangGraphAuditExecutor(
        temp_settings, model_bundle(SequenceModel(["处理完毕"]))
    ).run(audit_context())
    assert not result.file_results
    assert "报告未完成" in result.file_errors["bid-1"]
