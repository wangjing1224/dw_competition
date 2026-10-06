"""真实小窗口计数验证前缀路由、结构分批、交接压缩及末轮。"""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import StructuredTool
from langgraph.errors import NodeCancelledError

from app.audit.bid_summary import summarize_and_route_bid_document
from app.audit.session import run_session
from app.audit.sources import SourceLedger
from app.graph.audit_task_graph import LangGraphAuditExecutor
from app.llm.context_compaction import InputBudgetError
from app.llm.input_budget import InputContext, validate_tool_pairs
from app.models.documents import ParsedBlock
from app.reports.markdown_report import ReportDocument
from app.tools.bid_tools import build_evidence_review_tool
from app.tools.report_tools import report_tool
from tests.helpers.audit import (
    SequenceModel,
    audit_context,
    filled_report,
    model_bundle,
    parsed_file,
)
from tests.integration.test_audit_graph import ReportModel


@pytest.mark.asyncio
async def test_route_reads_only_complete_prefix(temp_settings):
    parsed = parsed_file("bid-1")
    parsed.sections[0].blocks = [
        ParsedBlock(
            kind="table",
            source_block_index=i,
            content=f"TABLE-{i}-BEGIN " + "a" * 6000 + f" TABLE-{i}-END",
        )
        for i in range(6)
    ]
    model = SequenceModel(
        ['{"match_status":"uncertain","match_reason":"前部项目身份不足"}']
    )
    cfg = temp_settings.bid_summary_model.model_copy(
        update={"context_window_tokens": 20_000, "tokenizer": "utf8"}
    )
    result = await summarize_and_route_bid_document(
        parsed, audit_context().rule_set, model, cfg
    )
    assert 0 < result.reading_end < result.total_units == 6
    human = model.calls[0][0][1].content
    assert "仅提供前部" in human
    for i in range(result.reading_end):
        assert f"TABLE-{i}-BEGIN" in human and f"TABLE-{i}-END" in human
    assert f"TABLE-{result.reading_end}-BEGIN" not in human
    assert len(model.calls) == 1


@pytest.mark.asyncio
async def test_main_batches_finish_all_structures_before_export(
    monkeypatch, temp_settings
):
    monkeypatch.setattr("app.graph.audit_task_graph.verify_indexes", lambda *args: {})
    context = audit_context()
    context.parsed_documents["bid-1"].sections[0].blocks = [
        ParsedBlock(
            kind="table",
            source_block_index=i,
            content=f"STRUCTURE-{i}-BEGIN " + "x" * 18000 + f" STRUCTURE-{i}-END",
        )
        for i in range(4)
    ]
    cfg = temp_settings.model_copy(
        update={
            "bid_chat_model": temp_settings.bid_chat_model.model_copy(
                # UTF8测试计数器按字节；包含新增工具与提示词，仍不足以放两个18K结构。
                update={"context_window_tokens": 65_000, "tokenizer": "utf8"}
            )
        }
    )
    model = ReportModel(context.rule_set)
    result = await LangGraphAuditExecutor(cfg, model_bundle(model)).run(context)
    assert not result.file_errors
    starts = [msgs for msgs in model.calls if not any(m.type == "tool" for m in msgs)]
    assert len(starts) > 1
    for i in range(4):
        assert sum(f"STRUCTURE-{i}-BEGIN" in msgs[1].content for msgs in starts) == 1
        assert sum(f"STRUCTURE-{i}-END" in msgs[1].content for msgs in starts) == 1
    assert "分辨率满足要求" in starts[1][1].content  # 下一批拿的是成功保存的报告。
    state = json.loads(
        (cfg.storage.data_root / "outputs/audit-1/drafts/bid-1.state.json").read_text()
    )
    assert state["next_unit_index"] == state["total_units"] == 4


@pytest.mark.asyncio
async def test_compaction_reinvokes_same_batch_without_resetting_limit(
    temp_settings, tmp_path
):
    context = audit_context()
    report = ReportDocument(
        tmp_path / "draft.md", context.rule_set, SourceLedger(context.parsed_documents)
    )

    async def large_result():
        return "X" * 90_000

    tools = [
        report_tool(report),
        StructuredTool.from_function(
            coroutine=large_result, description="读取测试材料"
        ),
    ]
    model = SequenceModel(
        [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "report_document",
                        "id": "save",
                        "type": "tool_call",
                        "args": {
                            "action": "write",
                            "content": filled_report(context.rule_set),
                            "edits": None,
                        },
                    },
                    {
                        "name": "large_result",
                        "id": "large",
                        "type": "tool_call",
                        "args": {},
                    },
                ],
            ),
            "核验和报告已保存，当前阅读范围不变，只需收尾。",
            "处理完毕",
        ]
    )
    stats = {}
    cfg = temp_settings.bid_chat_model.model_copy(
        update={"context_window_tokens": 100_000, "tokenizer": "utf8"}
    )
    await run_session(
        model=model,
        tools=tools,
        settings=cfg,
        max_steps=2,
        role="bid_agent",
        limiter=asyncio.Semaphore(1),
        trace_context={},
        report=report,
        stats=stats,
        build_messages=lambda handoff: [
            SystemMessage(content="规则固定"),
            HumanMessage(content="当前原文不变\n" + report.read() + "\n" + handoff),
        ],
    )
    assert report.finish()
    assert stats["model_calls"] == 2
    assert len(stats["compactions"]) == 1
    assert stats["compactions"][0]["step"] == 2
    assert len(model.calls) == 3 and len(model.bindings) == 1
    final = model.calls[-1][0]
    assert all(m.type in {"system", "human"} for m in final)
    assert len(final) == 3  # System、同批Human和既有末轮Human，无额外进度消息。
    assert "已无工具" in final[-1].content
    assert "## 当前草稿状态" not in final[1].content
    assert "当前原文不变" in final[1].content and "分辨率满足要求" in final[1].content
    assert "核验和报告已保存" in final[1].content


@pytest.mark.asyncio
async def test_fixed_input_too_large_does_not_call_model(temp_settings):
    model = SequenceModel([])
    cfg = temp_settings.bid_chat_model.model_copy(
        update={"context_window_tokens": 1000, "tokenizer": "utf8"}
    )
    with pytest.raises(InputBudgetError):
        await run_session(
            model=model,
            tools=[],
            settings=cfg,
            max_steps=8,
            role="bid_agent",
            limiter=asyncio.Semaphore(1),
            trace_context={},
            build_messages=lambda _: [
                SystemMessage(content="A" * 2000),
                HumanMessage(content="全文"),
            ],
        )
    assert not model.calls


async def run_tool_test(
    temp_settings, model, tools, role="bid_agent", max_steps=4, report=None
):
    return await run_session(
        model=model,
        tools=tools,
        settings=temp_settings.bid_chat_model,
        max_steps=max_steps,
        role=role,
        limiter=asyncio.Semaphore(1),
        trace_context={},
        report=report,
        build_messages=lambda _: [
            SystemMessage(content="工具测试"),
            HumanMessage(content="执行"),
        ],
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["bid_agent", "evidence_subagent"])
async def test_tool_errors_and_invalid_json_are_closed_then_corrected(
    temp_settings, role
):
    executed = []

    async def work(number: int):
        executed.append(number)
        if number == 1:
            raise RuntimeError("network unavailable")
        return f"完成{number}"

    tool = StructuredTool.from_function(coroutine=work, description="测试工具")
    calls = [
        {"name": "work", "id": "runtime", "args": {"number": 1}},
        {"name": "work", "id": "schema", "args": {"number": "oops"}},
        {"name": "unknown", "id": "unknown", "args": {}},
        {"name": "work", "id": "good", "args": {"number": 2}},
    ]
    response = AIMessage(
        content="",
        tool_calls=calls,
        invalid_tool_calls=[
            {"name": "work", "id": "json", "args": '{"number":', "error": "raw secret"}
        ],
        additional_kwargs={
            "tool_calls": [
                {
                    "id": key,
                    "type": "function",
                    "function": {"name": "work", "arguments": "{}"},
                }
                for key in ["json", "runtime", "schema", "unknown", "good"]
            ]
        },
        response_metadata={"finish_reason": "tool_calls"},
    )
    model = SequenceModel(
        [
            response,
            AIMessage(
                content="",
                tool_calls=[{"name": "work", "id": "fixed", "args": {"number": 3}}],
            ),
            "处理完毕",
        ]
    )
    assert await run_tool_test(temp_settings, model, [tool], role) == "处理完毕"
    feedback = [m for m in model.calls[1][0] if isinstance(m, ToolMessage)]
    assert [m.tool_call_id for m in feedback] == [
        "json",
        "runtime",
        "schema",
        "unknown",
        "good",
    ]
    assert [m.status for m in feedback] == ["error"] * 4 + ["success"]
    assert "JSON 解析失败" in feedback[0].content and "未执行" in feedback[0].content
    assert "raw secret" not in feedback[0].content
    assert "network unavailable" in feedback[1].content
    assert "number" in feedback[2].content
    assert executed == [1, 2, 3] and len(model.calls) == 3
    # SDK 仍能将非法调用重放为原始 arguments，与错误 ToolMessage 正确配对。
    wire = InputContext(tuple(model.calls[1][0])).wire_messages()
    validate_tool_pairs(wire)
    invalid = next(
        c for m in wire for c in m.get("tool_calls", []) if c["id"] == "json"
    )
    assert invalid["function"]["arguments"] == '{"number":'
    assert [m.type for m in model.calls[1][0]][:3] == ["system", "human", "ai"]


@pytest.mark.asyncio
@pytest.mark.parametrize("ids", [[None], ["same", "same"]])
async def test_unpairable_calls_stop_before_any_tool(temp_settings, ids):
    executed = []

    async def work():
        executed.append(True)
        return "done"

    model = SequenceModel(
        [
            AIMessage(
                content="",
                tool_calls=[{"name": "work", "args": {}, "id": key} for key in ids],
            )
        ]
    )
    with pytest.raises(RuntimeError, match="ID 缺失或重复"):
        await run_tool_test(
            temp_settings,
            model,
            [StructuredTool.from_function(coroutine=work, description="test")],
        )
    assert not executed


@pytest.mark.asyncio
async def test_cancel_and_mismatched_tool_result_remain_terminal(
    temp_settings, monkeypatch
):
    async def cancel():
        raise asyncio.CancelledError()

    tool = StructuredTool.from_function(coroutine=cancel, description="取消")
    response = AIMessage(
        content="", tool_calls=[{"name": "cancel", "id": "call", "args": {}}]
    )
    with pytest.raises(NodeCancelledError) as caught:
        await run_tool_test(temp_settings, SequenceModel([response]), [tool])
    assert isinstance(caught.value.__cause__, asyncio.CancelledError)

    async def wrong(*args):
        return ToolMessage(content="done", tool_call_id="wrong")

    monkeypatch.setattr("app.audit.session.invoke_agent_tool", wrong)
    with pytest.raises(RuntimeError, match="tool_call_id"):
        await run_tool_test(temp_settings, SequenceModel([response]), [tool])


@pytest.mark.asyncio
async def test_tool_error_feedback_does_not_claim_rollback_or_expose_credentials(
    temp_settings,
):
    written = []

    async def save_then_fail():
        written.append("persisted")
        raise OSError(
            "disk notification failed /Users/private/secret.json sk-secret123456 https://host.test?api_key=hidden"
        )

    tool = StructuredTool.from_function(coroutine=save_then_fail, description="写入")
    model = SequenceModel(
        [
            AIMessage(
                content="", tool_calls=[{"name": tool.name, "id": "write", "args": {}}]
            ),
            "无法确认，请核对",
        ]
    )
    await run_tool_test(temp_settings, model, [tool])
    feedback = next(m.content for m in model.calls[-1][0] if m.type == "tool")
    assert written == ["persisted"]
    assert "不能作为业务结论" in feedback and "不代表写入一定未生效" in feedback
    assert (
        "sk-secret" not in feedback
        and "/Users/" not in feedback
        and "host.test" not in feedback
    )


@pytest.mark.asyncio
async def test_invalid_json_on_final_tool_round_does_not_add_turns(temp_settings):
    model = SequenceModel(
        [
            AIMessage(
                content="",
                invalid_tool_calls=[{"name": "work", "id": "bad", "args": "{"}],
            ),
            "未能完成",
        ]
    )
    assert await run_tool_test(temp_settings, model, [], max_steps=2) == "未能完成"
    assert len(model.calls) == 2 and "已无工具" in model.calls[-1][0][-1].content


@pytest.mark.asyncio
async def test_invalid_report_arguments_can_be_corrected_without_losing_draft(
    temp_settings, tmp_path
):
    context = audit_context()
    report = ReportDocument(
        tmp_path / "draft.md", context.rule_set, SourceLedger(context.parsed_documents)
    )
    before = report.read()
    model = SequenceModel(
        [
            AIMessage(
                content="",
                invalid_tool_calls=[
                    {
                        "name": "report_document",
                        "id": "invalid",
                        "args": '{"action":"write","content":',
                    }
                ],
            ),
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "report_document",
                        "id": "corrected",
                        "args": {
                            "action": "write",
                            "content": filled_report(context.rule_set),
                            "edits": None,
                        },
                    }
                ],
            ),
            "处理完毕",
        ]
    )
    await run_tool_test(temp_settings, model, [report_tool(report)], report=report)
    feedback = next(m for m in model.calls[1][0] if m.type == "tool")
    assert feedback.tool_call_id == "invalid" and feedback.status == "error"
    assert "未执行" in feedback.content
    assert report.read() != before and report.finish() and not report.write_error


@pytest.mark.asyncio
async def test_child_model_failure_returns_parent_tool_error_and_allows_retry(
    temp_settings,
):
    context = audit_context()
    child = SequenceModel([RuntimeError("502 unavailable"), "事实已核实"])
    records = []
    tool = build_evidence_review_tool(
        context=context,
        settings=temp_settings,
        model=child,
        retrieval=SimpleNamespace(search=AsyncMock()),
        ledger=SourceLedger(context.parsed_documents),
        limiter=asyncio.Semaphore(1),
        trace_context={},
        records=records,
    )
    args = {
        "checks": [
            {
                "rule_ids": ["rule-1"],
                "verification_request": "核对参数",
                "file_ids": ["e-1"],
            }
        ]
    }
    model = SequenceModel(
        [
            AIMessage(
                content="",
                tool_calls=[{"name": tool.name, "id": "first", "args": args}],
            ),
            AIMessage(
                content="",
                tool_calls=[{"name": tool.name, "id": "retry", "args": args}],
            ),
            "处理完毕",
        ]
    )
    await run_tool_test(temp_settings, model, [tool])
    error = next(m for m in model.calls[1][0] if m.type == "tool")
    assert error.status == "error" and "502 unavailable" in error.content
    assert "不能作为业务结论" in error.content
    assert len(child.calls) == 2 and len(records) == 1


@pytest.mark.asyncio
async def test_subgraph_cancel_is_not_returned_as_retryable_tool_failure(
    temp_settings, monkeypatch
):
    async def cancelled_child(**kwargs):
        raise NodeCancelledError("子图取消")

    context = audit_context()
    tool = build_evidence_review_tool(
        context=context,
        settings=temp_settings,
        model=SequenceModel([]),
        retrieval=SimpleNamespace(search=AsyncMock()),
        ledger=SourceLedger(context.parsed_documents),
        limiter=asyncio.Semaphore(1),
        trace_context={},
        records=[],
    )
    monkeypatch.setattr("app.tools.bid_tools.run_session", cancelled_child)
    model = SequenceModel(
        [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": tool.name,
                        "id": "cancel",
                        "args": {
                            "checks": [
                                {
                                    "rule_ids": ["rule-1"],
                                    "verification_request": "核对",
                                    "file_ids": ["e-1"],
                                }
                            ]
                        },
                    }
                ],
            )
        ]
    )
    with pytest.raises(NodeCancelledError):
        await run_tool_test(temp_settings, model, [tool])
    assert len(model.calls) == 1
