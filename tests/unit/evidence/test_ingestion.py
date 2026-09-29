"""资料入库真实图与工具闭环；脚本模型无网络调用，数据全部在临时目录。"""

from dataclasses import replace
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from PIL import Image

from app.agents.evidence_ingestion_agent import prepare_evidence_request
from app.evidence.context import related_file_ids
from app.evidence.ingestion import ingest_evidence_documents
from app.evidence.storage import load_library, progress_path, write_library
from app.llm.token_counter import get_token_counter
from app.models.documents import (
    DocumentRef,
    ParsedBlock,
    ParsedDocument,
    ParsedSection,
    SourceRef,
)
from app.models.evidence import (
    EvidenceIngestionProgress,
    EvidenceLibrary,
    EvidenceMaterial,
    EvidenceSourceFile,
    SaveEvidenceMaterialInput,
)
from app.parsers.assets import AssetStore, file_asset_root
from app.retrieval.models import IndexFilter, SearchResult
from app.tools.common_tools import CommonTool
from app.tools.evidence_ingestion_tools import (
    EvidenceSession,
    build_evidence_ingestion_tools,
)


@pytest.fixture
def settings(temp_settings):
    return temp_settings.model_copy(
        update={
            "evidence_classification_model": temp_settings.evidence_classification_model.model_copy(
                update={
                    "platform": "deepseek",
                    "base_url": "https://api.deepseek.com/v1",
                    "model": "deepseek-v4-flash",
                    "tokenizer": "utf8",
                }
            )
        }
    )


def parsed(number, *, text=None):
    return ParsedDocument(
        schema_version=2,
        markdown_path=f"file-{number}/versions/v1/document.md",
        document=DocumentRef(
            file_id=f"file-{number}",
            file_name=f"材料{number}.md",
            source_path=f"资料/材料{number}.md",
            data_role="evidence_material",
            bidder_id="bidder-1",
        ),
        parse_status="parsed",
        sections=[
            ParsedSection(
                section_id="s1",
                title="材料正文",
                blocks=[
                    ParsedBlock(
                        kind="text",
                        source_block_index=0,
                        content=text or f"当前正文标记{number}；合同HT-001，设备20台。",
                    )
                ],
            )
        ],
    )


def library_for(*documents):
    return EvidenceLibrary(
        bidder_id="bidder-1",
        source_files=[
            EvidenceSourceFile(
                file_id=p.document.file_id,
                file_name=p.document.file_name,
                source_path=p.document.source_path,
            )
            for p in documents
        ],
    )


def material(*, relations=None, summary="合同HT-001，设备20台。"):
    return {
        "bid_summary": "设备供货合同，可核对业绩",
        "evidence_summary": summary,
        "relations": relations or [],
    }


def call(name, args, key="call-1"):
    return {"name": name, "args": args, "id": key, "type": "tool_call"}


def save(value=None, key="save-1"):
    return AIMessage(
        content="",
        tool_calls=[
            call(
                "save_evidence_material",
                {"material": value or material(), "file_ids": None},
                key,
            )
        ],
    )


class Model:
    def __init__(self, replies):
        self.replies = iter(replies)
        self.calls = []

    def bind_tools(self, tools):
        async def invoke(messages, **kwargs):
            return await self.invoke(messages, [t.name for t in tools], kwargs)

        return SimpleNamespace(ainvoke=invoke)

    async def ainvoke(self, messages, **kwargs):
        return await self.invoke(messages, [], kwargs)

    async def invoke(self, messages, tools, kwargs):
        self.calls.append((list(messages), tools, kwargs))
        assert kwargs["max_tokens"] > 0
        reply = next(self.replies)
        if isinstance(reply, Exception):
            raise reply
        return reply


async def ingest(settings, model, documents, **kwargs):
    return await ingest_evidence_documents(
        "bidder-1",
        documents,
        model,
        settings=settings,
        retrieval_service=SimpleNamespace(
            settings=settings, search=AsyncMock(return_value=[])
        ),
        **kwargs,
    )


def session(settings, current, history=(), library=None):
    return EvidenceSession(
        library=library or library_for(*history, current),
        current=current,
        history={p.document.file_id: p for p in history},
        settings=settings,
        counter=get_token_counter("utf8"),
        detail_budget_tokens=100000,
    )


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"material": None, "file_ids": None},
        {"material": None, "file_ids": []},
        {"material": material(), "file_ids": "all"},
        {"material": None, "file_ids": "file-1"},
        {"material": {**material(), "relations": [["", "file-1"]]}, "file_ids": None},
    ],
)
def test_tool_rejects_ambiguous_or_invalid_parameters(payload):
    with pytest.raises(ValueError):
        SaveEvidenceMaterialInput.model_validate(payload)


@pytest.mark.asyncio
async def test_serial_sessions_keep_current_body_and_free_relations(settings):
    model = Model(
        [
            save(),
            AIMessage(content="处理完毕"),
            save(
                material(relations=[["对该合同的交付情况作补充说明", "file-1"]]),
                "save-2",
            ),
            AIMessage(content="处理完毕"),
        ]
    )
    result = await ingest(settings, model, [parsed(1), parsed(2)])
    assert len(result.materials) == 2
    assert related_file_ids(result, "file-1") == ["file-2"]
    assert related_file_ids(result, "file-2") == ["file-1"]
    assert "file-2" not in str(model.calls[0][0])
    second = model.calls[2][0]
    assert [m.type for m in second] == ["system", "human"]
    assert "当前正文标记1" not in second[-1].content
    assert "合同HT-001" in second[0].content and "当前正文标记2" in second[-1].content
    progress = EvidenceIngestionProgress.model_validate_json(
        progress_path("bidder-1", settings).read_text()
    )
    assert progress.completed_file_ids == ["file-1", "file-2"]
    assert progress.error is None
    assert result == load_library("bidder-1", settings)


@pytest.mark.asyncio
async def test_save_snapshot_read_budget_and_relation_removal(settings):
    first, second = parsed(1), parsed(2)
    library = library_for(first, second)
    library.materials = [EvidenceMaterial(file_id="file-1", **material())]
    s = session(settings, second, [first], library)
    tool = build_evidence_ingestion_tools(s)[0]
    assert set(tool.tool_call_schema.model_json_schema()["properties"]) == {
        "material",
        "file_ids",
    }
    await tool.ainvoke(
        call(
            tool.name,
            {
                "material": material(
                    relations=[["引用其中编号", "file-1"], ["引用其中编号", "file-1"]]
                ),
                "file_ids": None,
            },
        )
    )
    assert len(s.library.materials[1].relations) == 1
    assert s.library.materials[0] == library.materials[0]
    before = load_library("bidder-1", settings)
    s.unresolved_write_error = "另一次提交失败"
    read = await tool.ainvoke(
        call(tool.name, {"material": None, "file_ids": "all"}, "read")
    )
    assert "file-1" in read.content and "file-2" not in read.content
    assert s.unresolved_write_error and load_library("bidder-1", settings) == before
    s.detail_budget_tokens = 1
    read = await tool.ainvoke(
        call(tool.name, {"material": None, "file_ids": "all"}, "limited")
    )
    assert "未展示：file-1" in read.content
    await tool.ainvoke(
        call(
            tool.name,
            {
                "material": material(summary="主体A；HT-001；补充金额100元"),
                "file_ids": None,
            },
            "update",
        )
    )
    assert related_file_ids(s.library, "file-1") == []
    assert s.unresolved_write_error is None


@pytest.mark.asyncio
@pytest.mark.parametrize("target", ["file-2", "file-3", "other-bidder-file"])
async def test_invalid_relation_does_not_modify_library(settings, target):
    s = session(settings, parsed(2))
    before = s.library.model_dump()
    with pytest.raises(ValueError):
        await s.apply(
            SaveEvidenceMaterialInput(
                material=material(relations=[["任意真实描述", target]]), file_ids=None
            ),
            "bad",
        )
    assert (
        s.library.model_dump() == before and load_library("bidder-1", settings) is None
    )


@pytest.mark.asyncio
async def test_failed_write_not_cleared_by_read_only_and_retry_succeeds(settings):
    bad = save(material(relations=[["补充", "file-99"]]))
    read = AIMessage(
        content="",
        tool_calls=[
            call(
                "save_evidence_material", {"material": None, "file_ids": "all"}, "read"
            )
        ],
    )
    model = Model([bad, read, AIMessage(content="处理完毕")])
    with pytest.raises(RuntimeError, match="尚未成功"):
        await ingest(settings, model, [parsed(1)])
    progress = EvidenceIngestionProgress.model_validate_json(
        progress_path("bidder-1", settings).read_text()
    )
    assert progress.next_unit_index == 0 and progress.unresolved_write_error
    result = await ingest(
        settings,
        Model([save(key="retry"), AIMessage(content="处理完毕")]),
        [parsed(1)],
        resume=True,
    )
    assert len(result.materials) == 1


@pytest.mark.asyncio
async def test_resume_keeps_saved_result_does_not_reread_completed_files(settings):
    docs = [parsed(1), parsed(2)]
    with pytest.raises(RuntimeError, match="network"):
        await ingest(
            settings,
            Model(
                [
                    save(),
                    AIMessage(content="处理完毕"),
                    save(key="two"),
                    RuntimeError("network"),
                ]
            ),
            docs,
        )
    assert len(load_library("bidder-1", settings).materials) == 2
    retry = Model([AIMessage(content="处理完毕")])
    result = await ingest(settings, retry, docs, resume=True)
    assert len(retry.calls) == 1 and "file-2" in retry.calls[0][0][1].content
    assert len(result.materials) == 2  # 已有结果、本次无变化，不强制再保存。
    changed = parsed(2, text="解析内容改变")
    with pytest.raises(ValueError, match="变化"):
        await ingest(settings, Model([]), [docs[0], changed], resume=True)
    with pytest.raises(ValueError, match="追加"):
        await ingest(settings, Model([]), docs)


@pytest.mark.asyncio
async def test_final_round_unbound_and_no_empty_success(settings):
    limited = settings.model_copy(update={"evidence_ingestion_max_steps": 2})
    model = Model([save(), AIMessage(content="处理完毕")])
    await ingest(limited, model, [parsed(1)])
    assert model.calls[0][1] and model.calls[1][1] == []
    assert (
        model.calls[0][0][-1].type == "human"
        and "最后一轮可调用" in model.calls[0][0][-1].content
    )
    assert "不提供工具" in model.calls[1][0][-1].content


@pytest.mark.asyncio
@pytest.mark.parametrize("finish_reason", [None, "stop"])
@pytest.mark.parametrize(
    "content", ["摘要已保存，本批阅读完成。", "Saved successfully.\n处理完毕"]
)
async def test_completion_uses_saved_material_not_exact_phrase(
    settings, content, finish_reason
):
    model = Model(
        [
            save(),
            AIMessage(
                content=content, response_metadata={"finish_reason": finish_reason}
            ),
        ]
    )
    result = await ingest(settings, model, [parsed(1)])
    assert len(result.materials) == 1 and len(model.calls) == 2
    progress = EvidenceIngestionProgress.model_validate_json(
        progress_path("bidder-1", settings).read_text()
    )
    assert progress.completed_file_ids == ["file-1"] and progress.error is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [
        AIMessage(content="已保存", response_metadata={"finish_reason": "length"}),
        AIMessage(
            content="已保存", response_metadata={"finish_reason": "content_filter"}
        ),
        AIMessage(content="已保存", response_metadata={"finish_reason": "tool_calls"}),
        AIMessage(content=""),
        AIMessage(content=" \n\t"),
        AIMessage(
            content="已保存",
            invalid_tool_calls=[
                {"name": "save_evidence_material", "id": "bad", "args": "{"}
            ],
        ),
    ],
)
async def test_saved_material_does_not_hide_abnormal_or_empty_reply(settings, response):
    with pytest.raises(RuntimeError):
        await ingest(settings, Model([save(), response]), [parsed(1)])
    progress = EvidenceIngestionProgress.model_validate_json(
        progress_path("bidder-1", settings).read_text()
    )
    assert progress.error and not progress.completed_file_ids
    assert progress.next_unit_index == 0
    assert len(load_library("bidder-1", settings).materials) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("content", ["处理完毕", "摘要已保存，本批阅读完成。"])
async def test_no_saved_file_cannot_complete(settings, content):
    with pytest.raises(RuntimeError, match="没有已保存结果"):
        await ingest(settings, Model([AIMessage(content=content)]), [parsed(1)])


def test_prepared_round_reminder_is_not_duplicated(settings):
    state = {"messages": [HumanMessage(content="阅读内容")], "model_calls": 6}
    ctx = prepare_evidence_request(
        state,
        tools=[],
        max_steps=8,
        model_settings=settings.evidence_classification_model,
    )
    rebuilt = prepare_evidence_request(
        {**state, "messages": list(ctx.messages), "prepared_step": 7},
        tools=[],
        max_steps=8,
        model_settings=settings.evidence_classification_model,
    )
    assert len(rebuilt.messages) == len(ctx.messages)


@pytest.mark.asyncio
async def test_history_image_path_can_be_viewed_without_text_search(settings):
    first = parsed(1)
    buf = BytesIO()
    Image.new("RGB", (24, 24), "white").save(buf, "PNG")
    image = AssetStore(
        file_asset_root(settings.storage.data_root, "file-1")
    ).save_image(buf.getvalue())
    first.sections[0].blocks = [
        ParsedBlock(kind="image", source_block_index=0, images=[image])
    ]
    library = library_for(first, parsed(2))
    library.materials = [
        EvidenceMaterial(
            file_id="file-1",
            **material(summary=f"扫描合同，编号不清，可回看{image.path}"),
        )
    ]
    s = session(settings, parsed(2), [first], library)
    view = await s.apply(
        SaveEvidenceMaterialInput(material=None, file_ids=["file-1"]), "read"
    )
    assert image.path in view
    image_tool = CommonTool(parsed_documents=s.history, settings=settings).tools()[0]
    result = await image_tool.ainvoke(call("view_image", {"path": image.path}))
    assert any(block["type"] == "image_url" for block in result.content)
    with pytest.raises(ValueError, match="未登记"):
        await s.apply(
            SaveEvidenceMaterialInput(
                material=material(summary="看file-2/images/999.png"), file_ids=None
            ),
            "fake",
        )


@pytest.mark.asyncio
async def test_common_retrieval_uses_history_default_checks_bidder_and_version(
    settings,
):
    first, current = parsed(1), parsed(2)
    hit = SearchResult(
        chunk_id="chunk-1",
        score=1,
        bidder_id="bidder-1",
        content="HT-001原文",
        source=SourceRef(
            file_id="file-1", file_name="材料1.md", source_path="资料/材料1.md"
        ),
        parse_version=first.markdown_path,
    )
    retrieval = SimpleNamespace(search=AsyncMock(return_value=[hit]))
    tool = CommonTool(
        parsed_documents={"file-1": first, "file-2": current},
        settings=settings,
        retrieval=retrieval,
        query_filter=IndexFilter(
            scope_id="evidence-bidder-1",
            bidder_id="bidder-1",
            data_role="evidence_material",
        ),
        default_file_ids=["file-1"],
        current_file_id="file-2",
    ).search_tool()
    result = await tool.ainvoke(
        call(
            tool.name,
            {"querylist": [{"queries": ["合同", "合同编号"], "file_ids": None}]},
        )
    )
    assert result.content.count("HT-001原文") == 1 and "chunk-1" in result.content
    assert retrieval.search.call_args.args[1].file_ids == ["file-1"]
    hit.bidder_id = "bidder-2"
    result = await tool.ainvoke(
        call(tool.name, {"querylist": [{"queries": ["合同"], "file_ids": ["file-1"]}]})
    )
    assert "越界" in result.content and "HT-001原文" not in result.content
    result = await tool.ainvoke(
        call(tool.name, {"querylist": [{"queries": ["合同"], "file_ids": ["file-3"]}]})
    )
    assert "越界" in result.content


@pytest.mark.asyncio
async def test_disk_failure_keeps_old_state(settings, monkeypatch):
    s = session(settings, parsed(1))
    write_library(s.library, settings)
    old = s.library

    def fail(*args):
        raise OSError("disk full")

    monkeypatch.setattr("app.tools.evidence_ingestion_tools.write_library", fail)
    with pytest.raises(OSError):
        await s.apply(
            SaveEvidenceMaterialInput(material=material(), file_ids=None), "write"
        )
    assert s.library == old == load_library("bidder-1", settings)


@pytest.mark.asyncio
async def test_only_explicit_unsupported_parse_can_skip_model(settings):
    doc = parsed(1)
    doc.sections = []
    doc.parse_status = "unsupported"
    result = await ingest(settings, Model([]), [doc])
    assert "未完成内容解读" in result.materials[0].bid_summary


@pytest.mark.asyncio
async def test_compaction_reinvokes_same_batch_without_resetting_round(
    settings, monkeypatch
):
    from app.llm.context_compaction import InputController

    original = InputController.check
    triggered = False

    def force_one_compaction(controller, context):
        nonlocal triggered
        decision = original(controller, context)
        if not triggered and any(
            isinstance(message, ToolMessage) for message in context.messages
        ):
            triggered = True
            return replace(decision, status="over_budget", reason="测试一次交接")
        return decision

    monkeypatch.setattr(InputController, "check", force_one_compaction)
    limited = settings.model_copy(update={"evidence_ingestion_max_steps": 3})
    read = AIMessage(
        content="",
        tool_calls=[
            call("save_evidence_material", {"material": None, "file_ids": "all"})
        ],
    )
    model = Model(
        [
            read,
            AIMessage(content="历史为空，当前合同待保存；没有已保存修改。"),
            save(),
            AIMessage(content="处理完毕"),
        ]
    )
    result = await ingest(limited, model, [parsed(1)])
    assert len(result.materials) == 1
    assert model.calls[1][1] == []  # 交接摘要无工具。
    assert model.calls[2][1] and "下一轮为 2/3" in str(model.calls[2][0])
    assert model.calls[3][1] == []  # 第3轮仍然最终轮，没有因重建清零。
    progress = EvidenceIngestionProgress.model_validate_json(
        progress_path("bidder-1", settings).read_text()
    )
    assert (
        progress.compaction_calls == 1
        and progress.model_calls == 3
        and progress.file_model_calls == 3
    )


@pytest.mark.asyncio
async def test_real_budget_splits_whole_blocks_and_allows_unchanged_last_batch(
    settings,
):
    doc = parsed(1, text="甲" * 8000)
    doc.sections[0].blocks.append(
        ParsedBlock(
            kind="table",
            source_block_index=1,
            content="<table><tr><td>" + "乙" * 8000 + "</td></tr></table>",
        )
    )
    small = settings.model_copy(
        update={
            "evidence_classification_model": settings.evidence_classification_model.model_copy(
                update={"context_window_tokens": 45000}
            )
        }
    )
    model = Model(
        [
            save(),
            AIMessage(content="本批摘要已保存。"),
            AIMessage(content="后续表格未带来新事实，保留已保存摘要。"),
        ]
    )
    await ingest(small, model, [doc])
    assert len(model.calls) == 3
    assert "乙" * 100 not in str(model.calls[0][0])
    assert "<table>" in str(model.calls[2][0]) and "</table>" in str(model.calls[2][0])
    assert "甲" * 100 not in str(model.calls[2][0])
    assert [m.type for m in model.calls[2][0]] == ["system", "human"]
    progress = EvidenceIngestionProgress.model_validate_json(
        progress_path("bidder-1", settings).read_text()
    )
    assert (
        progress.batch_index == 2
        and progress.model_calls == 1
        and progress.next_unit_index == 2
    )


@pytest.mark.asyncio
async def test_all_tool_calls_are_closed_including_errors_and_image_messages(settings):
    doc = parsed(1)
    buf = BytesIO()
    Image.new("RGB", (24, 24), "white").save(buf, "PNG")
    image = AssetStore(
        file_asset_root(settings.storage.data_root, "file-1")
    ).save_image(buf.getvalue())
    doc.sections[0].blocks.append(
        ParsedBlock(kind="image", source_block_index=1, images=[image])
    )
    image_call = AIMessage(
        content="",
        tool_calls=[
            call(
                "save_evidence_material", {"material": None, "file_ids": "all"}, "read"
            ),
            call("view_image", {"path": image.path}, "image"),
            call("view_image", {"path": "file-99/images/1.png"}, "denied"),
        ],
    )
    model = Model(
        [
            image_call,
            save(material(summary=f"合同HT-001；扫描附件可回看{image.path}")),
            AIMessage(content="处理完毕"),
        ]
    )
    await ingest(settings, model, [doc])
    outputs = [
        message for message in model.calls[1][0] if isinstance(message, ToolMessage)
    ]
    assert [message.tool_call_id for message in outputs] == ["read", "image", "denied"]
    assert any(block["type"] == "image_url" for block in outputs[1].content)
    assert outputs[2].status == "error" and "不能视为" in outputs[2].content


def test_history_details_do_not_force_current_document_to_split(settings):
    from app.evidence.ingestion import _context, _with_history
    from app.llm.context_compaction import InputController
    from app.llm.input_budget import check_input, plan_document_input
    from app.llm.model_capabilities import get_model_capabilities
    from app.parsers.reading import build_document_units

    small = settings.model_copy(
        update={
            "evidence_classification_model": settings.evidence_classification_model.model_copy(
                update={"context_window_tokens": 65000}
            )
        }
    )
    # 只验证新会话的历史选择，不让虚构长摘要输出触发另一种交互压缩。
    history_detail = "历史详情" * 2300
    current_body = "当前正文" * 2700
    first, current = parsed(1), parsed(2, text=current_body)
    library = library_for(first, current)
    library.materials = [
        EvidenceMaterial(file_id="file-1", **material(summary=history_detail))
    ]
    s = session(small, current, [first], library)
    tools = [
        *CommonTool(
            parsed_documents={"file-1": first, "file-2": current},
            settings=small,
            retrieval=SimpleNamespace(search=AsyncMock()),
            query_filter=IndexFilter(
                scope_id="evidence-bidder-1",
                data_role="evidence_material",
                bidder_id="bidder-1",
            ),
            default_file_ids=["file-1"],
            current_file_id="file-2",
        ).tools(),
        *build_evidence_ingestion_tools(s),
    ]
    controller = InputController(
        small.evidence_classification_model,
        s.counter,
        get_model_capabilities(small.evidence_classification_model),
    )
    progress = EvidenceIngestionProgress(
        bidder_id="bidder-1",
        file_ids=["file-1", "file-2"],
        parser_versions={},
        batch_index=1,
    )
    units = build_document_units(current)
    batch = plan_document_input(
        units=units,
        cursor=0,
        build_context=lambda start, end: _context(
            s, units, start, end, tools, progress
        ),
        budget_settings=controller.budget,
        counter=controller.counter,
    )
    assert batch.status == "ready" and batch.end == len(units)
    detailed = _context(s, units, 0, batch.end, tools, progress, detailed=True)
    assert (
        check_input(
            context=detailed,
            budget_settings=controller.budget,
            counter=controller.counter,
        ).status
        != "ready"
    )
    context = _with_history(s, units, 0, batch.end, tools, progress, controller)
    assert current_body in context.messages[-1].content
    assert history_detail not in context.messages[0].content
    assert (
        "file-1" in context.messages[0].content
        and "设备供货合同" in context.messages[0].content
    )
    assert controller.check(context).status == "ready"


@pytest.mark.asyncio
async def test_empty_library_initialization_can_resume_but_missing_progress_is_not_guessed(
    settings,
):
    doc = parsed(1)
    write_library(library_for(doc), settings)
    result = await ingest(
        settings, Model([save(), AIMessage(content="处理完毕")]), [doc], resume=True
    )
    assert len(result.materials) == 1
    # 不损坏进度文件来模拟缺失，使用另一个隔离输出根目录放入已有结果。
    other = settings.model_copy(
        update={
            "storage": settings.storage.model_copy(
                update={"data_root": settings.storage.data_root / "missing-progress"}
            )
        }
    )
    write_library(result, other)
    with pytest.raises(ValueError, match="恢复进度丢失"):
        await ingest(other, Model([]), [doc], resume=True)
