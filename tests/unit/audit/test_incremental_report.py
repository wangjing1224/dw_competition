"""假模型走真实会话/工具：增量写、内容纠错都使用原有轮次，不请求外部模型。"""

import asyncio

import pytest
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.tools import StructuredTool

from app.audit.session import run_session
from app.audit.sources import SourceLedger
from app.reports.markdown_report import TITLE_PLACEHOLDER, ReportDocument
from app.tools.report_tools import report_tool
from tests.helpers.audit import (
    SequenceModel,
    audit_context,
    filled_report,
    tool_response,
)


def edit_call(edits, call_id):
    return tool_response(
        "report_document",
        {
            "action": "edit",
            "content": None,
            "edits": [{"old_text": old, "new_text": new} for old, new in edits],
        },
        call_id,
    )


async def run(model, report, settings, tools=None, max_steps=3):
    return await run_session(
        model=model,
        tools=tools or [report_tool(report)],
        settings=settings.bid_chat_model,
        max_steps=max_steps,
        role="bid_agent",
        limiter=asyncio.Semaphore(1),
        trace_context={},
        build_messages=lambda _: [
            SystemMessage(content="测试审核"),
            HumanMessage(content=report.read()),
        ],
        report=report,
    )


@pytest.mark.asyncio
async def test_save_confirmed_part_and_query_an_independent_part_in_same_turn(
    tmp_path, temp_settings
):
    context = audit_context()
    context.rule_set.rules.append(
        context.rule_set.rules[0].model_copy(update={"rule_id": "rule-2"})
    )
    report = ReportDocument(
        tmp_path / "draft.md",
        context.rule_set,
        SourceLedger(context.parsed_documents),
        version=2,
    )
    body = "结论：符合\n实际发现：响应表写明参数。\n审核说明：满足要求。"
    first = edit_call([("### rule-1\n\n待填写。", "### rule-1\n\n" + body)], "partial")

    async def query_other():
        assert body in report.read() and "### rule-2\n\n待填写。" in report.read()
        return "另一项已查明"

    query = StructuredTool.from_function(
        coroutine=query_other, description="查询独立事项"
    )
    first.tool_calls.append(
        {"name": query.name, "args": {}, "id": "query", "type": "tool_call"}
    )
    model = SequenceModel(
        [
            first,
            edit_call(
                [
                    ("### rule-2\n\n待填写。", "### rule-2\n\n" + body),
                    (f"# {TITLE_PLACEHOLDER}", "# 设备审核报告"),
                    ("## 总体总结\n\n待填写。", "## 总体总结\n\n两项均符合。"),
                    ("## 重点问题\n\n待填写。", "## 重点问题\n\n无重点问题。"),
                ],
                "complete",
            ),
            "本批已保存。",
        ]
    )
    await run(model, report, temp_settings, [report_tool(report), query], max_steps=8)
    assert len(report.finish().categories[0].rules) == 2
    assert len(model.calls) == 3 and len(model.bindings) == 3  # 可提前结束，不跑满8轮。
    second = model.calls[1][0]
    assert [m.type for m in second] == ["system", "human", "ai", "tool", "tool"]
    feedback, query_result = [m for m in second if m.type == "tool"]
    assert feedback.tool_call_id == "partial"
    assert feedback.content.startswith("## 当前草稿状态\n已填写规则 1/2")
    assert query_result.tool_call_id == "query" and query_result.content == "另一项已查明"
    for messages, _ in model.calls:
        assert len([m for m in messages if m.type == "human"]) == 1
    final_tools = [m for m in model.calls[-1][0] if m.type == "tool"]
    assert final_tools[0].content == feedback.content  # 不重写历史Tool状态。
    assert final_tools[-1].content.startswith("## 当前草稿状态\n已填写规则 2/2")


@pytest.mark.asyncio
async def test_invalid_saved_content_can_be_edited_next_turn(tmp_path, temp_settings):
    context = audit_context()
    report = ReportDocument(
        tmp_path / "draft.md",
        context.rule_set,
        SourceLedger(context.parsed_documents),
        version=2,
    )
    invalid = filled_report(context.rule_set, version=2) + "\n投标片段：fake\n"
    model = SequenceModel(
        [
            tool_response(
                "report_document",
                {"action": "write", "content": invalid, "edits": None},
                "draft",
            ),
            edit_call([("\n投标片段：fake\n", "")], "repair"),
            "已修正并保存。",
        ]
    )
    await run(model, report, temp_settings, max_steps=8)
    assert report.finish()
    assert len(model.calls) == 3
    second = model.calls[1][0]
    feedback = next(m for m in second if m.type == "tool")
    assert feedback.status == "success"  # 保存成功，但内容尚不能定稿。
    assert feedback.content.startswith("## 当前草稿状态\n草稿校验未通过：")
    assert "草稿已保存" in feedback.content and "fake" in feedback.content
    assert [m.type for m in second] == ["system", "human", "ai", "tool"]


@pytest.mark.asyncio
async def test_query_only_rounds_keep_history_and_only_existing_final_reminders(
    tmp_path, temp_settings, monkeypatch
):
    context = audit_context()
    report = ReportDocument(
        tmp_path / "draft.md",
        context.rule_set,
        SourceLedger(context.parsed_documents),
        version=2,
    )

    def unexpected_progress():
        pytest.fail("没有报告工具调用时，不应生成额外草稿进度")

    monkeypatch.setattr(report, "progress", unexpected_progress)

    async def query():
        return "测试检索结果"

    tool = StructuredTool.from_function(coroutine=query, description="只读测试查询")
    model = SequenceModel(
        [tool_response(tool.name, {}, f"query-{i}") for i in range(1, 8)]
        + ["尚未完成报告填写。"]
    )
    await run(model, report, temp_settings, [tool], max_steps=8)
    assert len(model.calls) == 8 and len(model.bindings) == 7
    for step, (messages, _) in enumerate(model.calls, 1):
        humans = [m for m in messages if m.type == "human"]
        assert len(humans) == 1 + (step >= 7) + (step == 8)
        assert [m.tool_call_id for m in messages if m.type == "tool"] == [
            f"query-{i}" for i in range(1, step)
        ]
        assert all(
            m.content == "测试检索结果" for m in messages if m.type == "tool"
        )
    assert "最后一个工具轮" in model.calls[6][0][-1].content
    assert "已无工具" in model.calls[7][0][-1].content
    with pytest.raises(ValueError, match="报告未完成"):
        report.finish()


@pytest.mark.asyncio
async def test_invalid_last_write_does_not_grant_extra_tool_round(
    tmp_path, temp_settings
):
    context = audit_context()
    report = ReportDocument(
        tmp_path / "draft.md",
        context.rule_set,
        SourceLedger(context.parsed_documents),
        version=2,
    )
    model = SequenceModel(
        [
            tool_response(
                "report_document",
                {"action": "write", "content": "尚未修好的草稿", "edits": None},
            ),
            "无法在剩余轮次完成修复。",
        ]
    )
    await run(model, report, temp_settings, max_steps=2)
    assert len(model.calls) == 2 and len(model.bindings) == 1
    assert "已无工具" in model.calls[-1][0][-1].content
    assert report.read() == "尚未修好的草稿"
    with pytest.raises(ValueError, match="类别或规则集合"):
        report.finish()
