"""主 Agent 委派、真实图片 ToolMessage、读取登记和独立报告导出。"""

import asyncio
from io import BytesIO
from types import SimpleNamespace

import pytest
from docx import Document
from langchain_core.messages import AIMessage
from PIL import Image

from app.graph.audit_task_graph import LangGraphAuditExecutor
from app.models.documents import PageSource, ParsedBlock
from app.parsers.assets import AssetStore, file_asset_root
from app.reports.json_report import write_json_result
from app.reports.word_report import generate_word_reports
from app.retrieval.models import SearchResult
from app.storage.file_store import FileStore
from tests.helpers.audit import (
    SequenceModel,
    audit_context,
    filled_report,
    tool_response,
)


@pytest.mark.asyncio
async def test_delegate_search_image_and_report_without_extra_llm(
    monkeypatch, temp_settings
):
    context = audit_context()
    buffer = BytesIO()
    Image.new("RGB", (80, 40), "white").save(buffer, format="PNG")
    asset = AssetStore(
        file_asset_root(temp_settings.storage.data_root, "e-2")
    ).save_image(buffer.getvalue())
    context.parsed_documents["e-2"].sections[0].blocks = [
        ParsedBlock(
            kind="image",
            source_block_index=0,
            images=[asset],
            page_sources=[PageSource(page_number=2, image_path=asset.path)],
        )
    ]
    context.evidence_library.materials[1].evidence_summary += " 图片位置：" + asset.path
    monkeypatch.setattr("app.graph.audit_task_graph.verify_indexes", lambda *args: {})
    hit = SearchResult(
        chunk_id="chunk-e-1-v0123456789abcdef",
        score=1,
        content="合同数量为10台。",
        bidder_id="bidder-1",
        source={
            "file_id": "e-1",
            "file_name": "e-1.md",
            "source_path": "e-1.md",
            "section": "合同",
        },
        parse_version=context.parsed_documents["e-1"].markdown_path,
    )
    calls = []

    async def search(query, scope):
        calls.append(scope)
        return [hit]

    monkeypatch.setattr(
        "app.graph.audit_task_graph.RetrievalService",
        lambda *args, **kwargs: SimpleNamespace(search=search),
    )
    child = SequenceModel(
        [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "search_documents",
                        "id": "search",
                        "type": "tool_call",
                        "args": {
                            "querylist": [
                                {"queries": ["合同数量"], "file_ids": ["e-1"]}
                            ]
                        },
                    },
                    {
                        "name": "view_image",
                        "id": "image",
                        "type": "tool_call",
                        "args": {"path": asset.path},
                    },
                ],
            ),
            f"资料不支持全部交付声明。合同（{hit.chunk_id}）和验收图片（{asset.path}）只支持10台。",
        ]
    )
    draft = filled_report(
        context.rule_set,
        reason="合同及验收记录不支持全部交付声明。",
        conclusion="不符合",
    )
    draft += f"资料片段：{hit.chunk_id}\n图片来源：{asset.path}\n"
    parent = SequenceModel(
        [
            tool_response(
                "request_evidence_review",
                {
                    "checks": [
                        {
                            "rule_ids": ["rule-1"],
                            "verification_request": "核实12台全部交付的声明",
                            "file_ids": ["e-1"],
                        }
                    ]
                },
            ),
            tool_response(
                "report_document",
                {"action": "write", "content": draft, "edits": None},
                "save",
            ),
            "处理完毕",
        ]
    )
    cfg = temp_settings.model_copy(
        update={
            "audit": temp_settings.audit.model_copy(update={"model_concurrency": 1})
        }
    )
    result = await asyncio.wait_for(
        LangGraphAuditExecutor(
            cfg,
            SimpleNamespace(
                bid_chat=parent,
                evidence_chat=child,
                embedding=object(),
                rerank=object(),
            ),
        ).run(context),
        timeout=10,
    )
    assert not result.file_errors
    assert len(parent.calls) == 3 and len(child.calls) == 2
    assert set(calls[0].file_ids) == {"e-1"}
    image_message = next(
        m for m in child.calls[1][0] if m.type == "tool" and m.name == "view_image"
    )
    assert image_message.content[1]["image_url"]["url"].startswith(
        "data:image/png;base64,"
    )
    assert "data:image" not in str(parent.calls[1][0])
    assert hit.chunk_id in str(parent.calls[1][0]) and asset.path in str(parent.calls[1][0])
    file_result = result.file_results[0]
    assert file_result.sources[asset.path].page_numbers == [2]
    assert file_result.findings[0].image_paths == [asset.path]
    assert "data:image" not in result.model_dump_json()

    monkeypatch.setattr("app.storage.file_store.get_settings", lambda: temp_settings)
    store = FileStore()
    path = write_json_result(context.task_id, result, store)
    word_path = store.resolve_relative(generate_word_reports(path, store)[0])
    text = "\n".join(p.text for p in Document(word_path).paragraphs)
    assert "e-2.md" in text and "页码：2" in text and asset.path in text
    assert "合同数量为10台" in text and "non_compliant" not in text
    assert "资料1简洁目录" not in text and "资料1详细事实" not in text


@pytest.mark.asyncio
async def test_main_reads_human_image_directly_but_cannot_read_evidence(
    monkeypatch, temp_settings
):
    context = audit_context()
    context.report_format_version = 2
    buffer = BytesIO()
    Image.new("RGB", (80, 40), "white").save(buffer, format="PNG")
    images = {}
    for file_id in ("bid-1", "e-1"):
        asset = AssetStore(file_asset_root(temp_settings.storage.data_root, file_id)).save_image(buffer.getvalue())
        images[file_id] = asset.path
        context.parsed_documents[file_id].sections[0].blocks.append(
            ParsedBlock(kind="image", source_block_index=1, images=[asset])
        )
    monkeypatch.setattr("app.graph.audit_task_graph.verify_indexes", lambda *args: {})

    async def no_search(*args):
        pytest.fail("Human 已有图片入口，不要求先搜索")

    monkeypatch.setattr(
        "app.graph.audit_task_graph.RetrievalService",
        lambda *args, **kwargs: SimpleNamespace(search=no_search),
    )
    draft = filled_report(context.rule_set, version=2) + f"\n图片来源：{images['bid-1']}\n"
    parent = SequenceModel([
        tool_response("view_image", {"path": images["e-1"]}, "denied"),
        tool_response("view_image", {"path": images["bid-1"]}, "visible"),
        tool_response("report_document", {"action": "write", "content": draft, "edits": None}, "save"),
        "已保存。",
    ])
    result = await LangGraphAuditExecutor(
        temp_settings,
        SimpleNamespace(bid_chat=parent, evidence_chat=object(), embedding=object(), rerank=object()),
    ).run(context)
    assert not result.file_errors
    human = next(m.content for m in parent.calls[0][0] if m.type == "human")
    assert images["bid-1"] in human
    assert "bid-1独立正文：分辨率640×480。" in human
    denied = next(m for m in parent.calls[1][0] if m.type == "tool")
    assert denied.status == "error" and "授权范围" in denied.content
    image_message = next(m for m in parent.calls[2][0] if m.type == "tool" and m.tool_call_id == "visible")
    assert image_message.content[1]["image_url"]["url"].startswith("data:image/png;base64,")
    assert set(result.file_results[0].sources) == {images["bid-1"]}
