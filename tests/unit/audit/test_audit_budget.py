"""真实小窗口计数验证前缀路由、结构分批、交接压缩及末轮。"""

import asyncio
import json

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_core.tools import StructuredTool

from app.audit.bid_summary import summarize_and_route_bid_document
from app.audit.session import run_session
from app.audit.sources import SourceLedger
from app.graph.audit_task_graph import LangGraphAuditExecutor
from app.llm.context_compaction import InputBudgetError
from app.models.documents import ParsedBlock
from app.reports.markdown_report import ReportDocument
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
                update={"context_window_tokens": 50_000, "tokenizer": "utf8"}
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
