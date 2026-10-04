"""公共读取授权、委派会话和通用报告编辑工具。"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from langchain_core.utils.function_calling import convert_to_openai_tool

from app.audit.context import evidence_view, scope_for
from app.audit.sources import SourceLedger
from app.reports.markdown_report import ReportDocument, parse_report
from app.retrieval.models import SearchResult
from app.tools.bid_tools import build_evidence_review_tool
from app.tools.common_tools import CommonTool
from app.tools.report_tools import report_tool
from tests.helpers.audit import SequenceModel, audit_context, filled_report


def call(name, args, id="call-1"):
    return {"name": name, "args": args, "id": id, "type": "tool_call"}


def test_search_description_uses_instance_configuration_without_changing_schema(
    temp_settings,
):
    context = audit_context()
    parsed = {"bid-1": context.parsed_documents["bid-1"]}
    descriptions = []
    for candidate, final in [(30, 10), (7, 3), (30, 10)]:
        settings = temp_settings.model_copy(
            update={
                "retrieval": temp_settings.retrieval.model_copy(
                    update={
                        "candidate_top_k": candidate,
                        "final_top_k": final,
                    }
                ),
            }
        )
        tool = CommonTool(
            parsed_documents=parsed,
            settings=settings,
            retrieval=SimpleNamespace(search=AsyncMock()),
            file_filters={key: scope_for(p.document) for key, p in parsed.items()},
            default_file_ids=["bid-1"],
        ).search_tool()
        descriptions.append(tool.description)
        assert f"各自最多取 {candidate} 个候选" in tool.description
        assert f"最终最多返回 {final} 个片段" in tool.description
        assert (
            "${" not in tool.description and "不按正文字符预算裁剪" in tool.description
        )
        schema = convert_to_openai_tool(tool)["function"]["parameters"]
        assert set(schema["properties"]) == {"querylist"}
        assert "candidate_top_k" not in str(schema) and "final_top_k" not in str(schema)
    assert descriptions[0] == descriptions[2] != descriptions[1]


@pytest.mark.asyncio
async def test_common_search_routes_roles_and_records_only_displayed(temp_settings):
    context = audit_context()
    parsed = {
        key: p
        for key, p in context.parsed_documents.items()
        if key in {"bid-1", "tender-1"}
    }
    ledger = SourceLedger(parsed)
    searches = []
    full_text = "实际展示原文" + "完整表格行\n" * 1000 + "完整末尾"

    async def search(query, scope):
        searches.append(scope)
        file_id = scope.file_ids[0]
        return [
            SearchResult(
                chunk_id=f"chunk-{file_id}-{query}",
                score=1,
                source={
                    "file_id": file_id,
                    "file_name": file_id + ".md",
                    "source_path": file_id + ".md",
                    "section": "正文",
                },
                content=full_text,
                parse_version=parsed[file_id].markdown_path,
                project_id=scope.project_id,
                bidder_id=scope.bidder_id,
                task_id=scope.task_id,
            )
        ]

    tool = CommonTool(
        parsed_documents=parsed,
        settings=temp_settings,
        retrieval=SimpleNamespace(search=search),
        file_filters={key: scope_for(p.document) for key, p in parsed.items()},
        default_file_ids=["bid-1"],
        on_chunk_read=ledger.chunk_read,
        strict_errors=True,
    ).search_tool()
    response = await tool.ainvoke(
        call(
            tool.name,
            {
                "querylist": [
                    {
                        "queries": ["分辨率", "分辨率", "数量"],
                        "file_ids": ["bid-1", "tender-1"],
                    }
                ]
            },
        )
    )
    assert response.content.count(full_text) == 4
    assert {s.scope_id for s in searches} == {"bid-audit-1", "rules-project-1"}
    assert set(ledger.sources) == {
        f"chunk-{file_id}-{query}"
        for file_id in ["bid-1", "tender-1"]
        for query in ["分辨率", "数量"]
    }
    searches.clear()
    response = await tool.ainvoke(
        call(tool.name, {"querylist": [{"queries": ["参数"], "file_ids": None}]})
    )
    assert len(searches) == 1 and searches[0].file_ids == ["bid-1"]
    searches.clear()
    response = await tool.ainvoke(
        call(tool.name, {"querylist": [{"queries": ["参数"], "file_ids": ["e-1"]}]})
    )
    assert "越界" in response.content and not searches


@pytest.mark.asyncio
async def test_model_failure_in_child_is_not_business_uncertainty(temp_settings):
    context = audit_context()
    tool = build_evidence_review_tool(
        context=context,
        settings=temp_settings,
        model=SequenceModel([RuntimeError("402 Balance")]),
        retrieval=SimpleNamespace(search=AsyncMock()),
        ledger=SourceLedger(context.parsed_documents),
        limiter=asyncio.Semaphore(1),
        trace_context={},
        records=[],
    )
    with pytest.raises(RuntimeError, match="402"):
        await tool.ainvoke(
            call(
                tool.name,
                {
                    "checks": [
                        {
                            "rule_id": "rule-1",
                            "verification_request": "确认分辨率",
                            "file_ids": ["e-1"],
                        }
                    ]
                },
            )
        )


@pytest.mark.asyncio
async def test_delegate_one_hop_raw_content_and_call_id_idempotency(temp_settings):
    context = audit_context()
    ledger = SourceLedger(context.parsed_documents)
    records = []
    model = SequenceModel(["信息不足。资料原文未读，无法确认。", "与声明一致。"])
    tool = build_evidence_review_tool(
        context=context,
        settings=temp_settings,
        model=model,
        retrieval=SimpleNamespace(search=AsyncMock()),
        ledger=ledger,
        limiter=asyncio.Semaphore(1),
        trace_context={},
        records=records,
    )
    args = {
        "checks": [
            {
                "rule_id": "rule-1",
                "verification_request": "确认分辨率",
                "file_ids": ["e-1"],
            },
            {
                "rule_id": "rule-1",
                "verification_request": "确认数量",
                "file_ids": ["e-3"],
            },
        ]
    }
    result = await tool.ainvoke(call(tool.name, args))
    assert "信息不足" in result.content and "与声明一致" in result.content
    assert len(records) == len(model.calls) == 2
    human = model.calls[0][0][1].content
    assert "资料1详细事实" in human and "资料2详细事实" in human
    assert "资料3详细事实" not in human and "rule-1" not in human
    assert not ledger.sources  # 子模型声称的来源不会凭空加入台账。
    assert (await tool.ainvoke(call(tool.name, args))).content == result.content
    assert len(model.calls) == 2
    schema = convert_to_openai_tool(tool)["function"]["parameters"]
    assert set(schema["properties"]) == {"checks"}
    assert "evidence_types" not in str(schema)


def test_one_hop_relations_do_not_expand_transitively():
    selected, relations, _ = evidence_view(audit_context().evidence_library, ["e-1"])
    assert selected == ["e-1", "e-2"]
    assert "e-2 → e-1" in relations


@pytest.mark.asyncio
async def test_report_tool_atomic_edit_idempotency_and_source_validation(tmp_path):
    context = audit_context()
    ledger = SourceLedger(context.parsed_documents)
    report = ReportDocument(tmp_path / "draft.md", context.rule_set, ledger)
    tool = report_tool(report)
    schema = convert_to_openai_tool(tool)["function"]["parameters"]
    assert set(schema["properties"]) == {"action", "content", "edits"}
    text = filled_report(context.rule_set)
    await tool.ainvoke(
        call(tool.name, {"action": "write", "content": text, "edits": None})
    )
    assert report.finish().categories[0].rules[0].result == "compliant"
    edit = {
        "action": "edit",
        "content": None,
        "edits": [{"old_text": "分辨率满足要求", "new_text": "核验事实明确"}],
    }
    result = await tool.ainvoke(call(tool.name, edit, "edit-1"))
    assert "已保存" in result.content
    assert (
        await tool.ainvoke(call(tool.name, edit, "edit-1"))
    ).content == result.content
    before = report.read()
    with pytest.raises(ValueError, match="未唯一命中"):
        await tool.ainvoke(
            call(
                tool.name,
                {
                    "action": "edit",
                    "content": None,
                    "edits": [
                        {"old_text": "核验事实明确", "new_text": "新文字"},
                        {"old_text": "不存在", "new_text": "无"},
                    ],
                },
                "edit-bad",
            )
        )
    assert report.read() == before

    with pytest.raises(ValueError, match="未解决"):
        report.finish()
    await tool.ainvoke(
        call(tool.name, {"action": "write", "content": before, "edits": None}, "fix")
    )
    assert report.finish()
    invalid = before + "图片来源：e-1/images/1.png\n"
    receipt = await tool.ainvoke(
        call(
            tool.name,
            {
                "action": "write",
                "content": invalid,
                "edits": None,
            },
            "image-fake",
        )
    )
    assert "草稿已保存" in receipt.content and "未实际读取" in receipt.content
    assert report.read() == invalid
    with pytest.raises(ValueError, match="未实际读取"):
        report.finish()


@pytest.mark.asyncio
async def test_new_batch_can_reuse_provider_tool_call_id(tmp_path):
    context = audit_context()
    report = ReportDocument(
        tmp_path / "draft.md", context.rule_set, SourceLedger(context.parsed_documents)
    )
    report.start_batch(1)
    await report.apply("write", filled_report(context.rule_set), None, "call-0")
    report.start_batch(2)
    await report.apply(
        "write",
        filled_report(context.rule_set, reason="后续原文补充的新判断"),
        None,
        "call-0",
    )
    assert "后续原文补充的新判断" in report.read()


@pytest.mark.parametrize(
    "transform",
    [
        lambda text: text.replace("rule-1", "rule-999"),
        lambda text: text + "### rule-1\n结论：符合\n审核说明：重复",
        lambda text: text.replace("结论：符合", "结论：好像行"),
        lambda text: text.replace("审核说明：分辨率满足要求", "审核说明："),
    ],
)
def test_invalid_report_is_rejected(transform):
    context = audit_context()
    with pytest.raises(ValueError):
        parse_report(
            transform(filled_report(context.rule_set)),
            context.rule_set,
            SourceLedger(context.parsed_documents),
            final=True,
        )
