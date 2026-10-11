"""R/H/L确定性验收；脚本模型/冻结索引，不连接外部服务。"""

import asyncio
import re
from types import SimpleNamespace
from unittest.mock import AsyncMock

import numpy as np
import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_core.tools import StructuredTool
from langchain_core.utils.function_calling import convert_to_openai_tool
from pydantic import ValidationError

from app.audit.context import scope_for
from app.audit.session import make_controller, prepare_request, run_session
from app.audit.sources import SourceLedger
from app.models.documents import ParsedImage
from app.models.rules import CandidateEdit, RuleCandidatesInput, RuleSet, RuleSourceFile, SubmitRuleDeltaInput
from app.reports.markdown_report import ReportDocument, parse_report
from app.retrieval.models import IndexChunk, IndexManifest
from app.retrieval.service import RetrievalService
from app.retrieval.store import LoadedIndex
from app.rules.context import CANDIDATE_ITEM_TEMPLATE, CANDIDATE_TEMPLATE, rule_working_context, validate_candidates
from app.tools.common_tools import CommonTool, SearchDocumentsInput
from app.tools.report_tools import ReportEdit, ReportSavePolicy, report_tool
from app.tools.rule_tools import RuleSession, active_rule_tools, build_rule_tools
from tests.helpers.audit import SequenceModel, audit_context, candidate_response, filled_report, parsed_file, tool_response


CANDIDATES = "# 当前文件候选草稿\n\n候选简介：期限要求。\n\n## 候选清单\n\n### 候选 1\n\n要求：90日。\n\n候选说明：本文件期限条款。\n"


def candidate_session(settings):
    return RuleSession(
        rule_set=RuleSet(project_id="project-1", project_profile="OLD_PROFILE_SENTINEL"),
        current=parsed_file("tender-1", "rule_source"), history={}, persist=AsyncMock(),
        retrieval=SimpleNamespace(settings=settings), rule_limit=100,
    )


@pytest.mark.parametrize("payload", [
    {}, {"action": "write"}, {"action": "edit", "edits": []},
    {"action": "read", "edits": []}, {"action": "finish", "extra": 1},
])
def test_candidate_schema_rejects_invalid_shapes(payload):
    with pytest.raises(ValidationError):
        RuleCandidatesInput.model_validate(payload)


@pytest.mark.asyncio
async def test_candidates_atomic_edit_finish_and_phase(temp_settings):
    s = candidate_session(temp_settings)
    tools = build_rule_tools(s)
    assert "OLD_PROFILE_SENTINEL" not in rule_working_context(s)
    assert "submit_rule_delta" not in [t.name for t in active_rule_tools(tools, s.phase)]
    tool = next(t for t in tools if t.name == "rule_candidates")
    assert set(convert_to_openai_tool(tool)["function"]["parameters"]["properties"]) == {"action", "edits", "return_view"}
    await tool.ainvoke({"action": "edit", "edits": [{"old_text": CANDIDATE_TEMPLATE, "new_text": CANDIDATES}]})
    assert s.phase == "extract" and validate_candidates(s.candidate_draft) == 1
    with pytest.raises(ValueError, match="匹配0处"):
        await tool.ainvoke({"action": "edit", "edits": [
            {"old_text": "90日", "new_text": "80日"}, {"old_text": "不存在", "new_text": "失败"}]})
    assert s.candidate_draft == CANDIDATES
    await tool.ainvoke({"action": "read"})
    with pytest.raises(ValueError, match="未解决"):
        await tool.ainvoke({"action": "finish"})
    await tool.ainvoke({"action": "edit", "edits": [{"old_text": "90日", "new_text": "90日"}]})
    s.persist.side_effect = OSError("disk")
    with pytest.raises(OSError):
        await tool.ainvoke({"action": "finish"})
    assert s.phase == "extract" and s.candidate_draft == CANDIDATES
    s.persist.side_effect = None
    result = await tool.ainvoke({"action": "finish"})
    assert s.phase == "merge" and CANDIDATES in result and "OLD_PROFILE_SENTINEL" in result
    assert "rule_candidates" not in [t.name for t in active_rule_tools(tools, s.phase)]


@pytest.mark.asyncio
async def test_candidate_invalid_saved_and_zero_candidates(temp_settings):
    s = candidate_session(temp_settings)
    result = await s.candidates("edit", [CandidateEdit(old_text=CANDIDATE_TEMPLATE, new_text="还未整理")])
    assert "结构待修正" in result and s.candidate_draft == "还未整理"
    with pytest.raises(ValueError):
        await s.candidates("finish")
    zero = "# 当前文件候选草稿\n\n候选简介：本批只有采购背景，未含内容核验要求。\n\n## 候选清单\n\n无候选。"
    await s.candidates("edit", [CandidateEdit(old_text="还未整理", new_text=zero)])
    await s.candidates("finish")
    assert validate_candidates(s.candidate_draft) == 0


@pytest.mark.asyncio
async def test_candidate_initial_input_and_tool_example_match_validator(temp_settings):
    from app.rules.extractor import _batch_context, build_document_units

    session = candidate_session(temp_settings)
    session.batch_index = 1
    tools = build_rule_tools(session)
    candidate_tool = next(t for t in tools if t.name == "rule_candidates")
    example = re.search(r"```markdown\n(.*?)\n```", candidate_tool.description, re.S)[1]
    assert example == CANDIDATE_ITEM_TEMPLATE
    context = _batch_context(
        session=session, units=build_document_units(session.current), start=0, end=1,
        tools=tools, settings=temp_settings,
    )
    assert CANDIDATE_TEMPLATE in context.messages[1].content
    assert example in context.messages[1].content
    # 从真实Human提取完整草稿，直接edit；无需先read或预先知道隐藏格式。
    visible_draft = re.search(
        r"^# 当前文件候选草稿\n.*?(?=\n\n## 已完成历史文件目录)",
        context.messages[1].content, re.M | re.S,
    )[0]
    assert visible_draft == CANDIDATE_TEMPLATE
    filled = visible_draft.replace("候选简介：待填写。", "候选简介：期限要求。")
    filled = filled.replace("要求：待填写。", "要求：90日。")
    filled = filled.replace("候选说明：待填写。", "候选说明：本文件期限条款。")
    assert validate_candidates(filled) == 1
    result = await candidate_tool.ainvoke({
        "action": "edit", "edits": [{"old_text": visible_draft, "new_text": filled}],
    })
    assert "结构校验通过，候选1项" in result
    assert session.candidate_draft == filled and session.phase == "extract"
    session.persist.assert_awaited_once()
    with pytest.raises(ValueError):
        validate_candidates(CANDIDATE_TEMPLATE)
    # 候选只读取本批输入，检索补查交给合并阶段。
    assert "search_documents" not in [t.name for t in active_rule_tools(tools, "extract")]


@pytest.mark.parametrize("phase", ["extract", "merge"])
@pytest.mark.parametrize("handoff", ["", "压缩后的工作交接"])
def test_rule_phase_search_binding_matches_system_and_human(temp_settings, phase, handoff):
    from app.rules.extractor import _batch_context, build_document_units

    session = candidate_session(temp_settings)
    session.phase, session.batch_index = phase, 1
    tools = [
        *build_rule_tools(session),
        *CommonTool(parsed_documents={session.current.document.file_id: session.current}, settings=temp_settings).tools(),
    ]
    context = _batch_context(
        session=session, units=build_document_units(session.current), start=0, end=1,
        tools=tools, settings=temp_settings, model_calls=3 if handoff else 0, handoff=handoff,
    )
    names = {t["function"]["name"] for t in context.bindings["tools"]}
    expected = {"rule_candidates"} if phase == "extract" else {"submit_rule_delta", "search_documents"}
    assert names == expected | {"view_image", "run_python"}
    system, human = (m.content for m in context.messages[:2])
    assert ("### search_documents" in system) == (phase == "merge")
    if phase == "extract":
        assert "本阶段不开放search_documents" in system
        assert "具体缺口" in system and "候选说明" in system
        assert "首轮已有充分事实时直接edit" in system
    else:
        assert "本工具仅在合并阶段开放" in system
        assert "不按候选主题把同一份正文再搜索一遍" in system
    assert "## 当前业务状态与完整候选草稿" in human
    assert human.count(session.candidate_draft) == 1
    assert "候选阶段不开放search_documents" in human
    assert "仅在finish交接后的合并阶段" in human
    assert "不允许查询尚未轮到的其他文件" in human
    assert "此处仅声明访问权限，不代表需要重新检索已给出的正文" in human
    assert "合并阶段可全文回查当前文件" not in human
    assert session.current.sections[0].blocks[0].content in human
    if handoff:
        assert "检索回查仅在合并阶段" in human


@pytest.mark.asyncio
@pytest.mark.parametrize("search_args", [
    {"querylist": [{"queries": ["期限"], "file_ids": ["tender-1"]}]},
    {"chunklist": [{"chunk_id": "real-chunk", "file_id": "tender-1", "before": 1, "after": 1}]},
])
async def test_candidate_search_rejected_until_next_merge_round(temp_settings, search_args):
    from app.graph.rule_graph import build_rule_graph
    from app.rules.extractor import _batch_context, build_document_units

    session = candidate_session(temp_settings)
    session.batch_index = 1
    searches = []

    async def search_documents(querylist=None, chunklist=None):
        searches.append((querylist, chunklist))
        return "合并阶段实际补查内容"

    search = StructuredTool.from_function(
        coroutine=search_documents, args_schema=SearchDocumentsInput, description="检索测试替身",
    )
    tools = [search if t.name == "search_documents" else t for t in build_rule_tools(session)]
    model = SequenceModel([
        AIMessage(content="", tool_calls=[
            tool_response("search_documents", search_args, "before-finish").tool_calls[0],
            *candidate_response("90日。").tool_calls,
            tool_response("search_documents", search_args, "same-round-after-finish").tool_calls[0],
        ]),
        tool_response("search_documents", search_args, "merge-search"),
        "阶段权限验证结束",
    ])
    context = _batch_context(
        session=session, units=build_document_units(session.current), start=0, end=1,
        tools=tools, settings=temp_settings,
    )
    graph = build_rule_graph(
        model=model, tools=tools, session=session, max_steps=temp_settings.rule_agent_max_steps,
        controller=make_controller(temp_settings.rule_extraction_model),
    )
    result = await graph.ainvoke({"messages": list(context.messages), "model_calls": 0})
    replies = {m.tool_call_id: m for m in result["messages"] if m.type == "tool"}
    for call_id in ("before-finish", "same-round-after-finish"):
        assert replies[call_id].status == "error"
        assert "本轮未授权工具 search_documents" in replies[call_id].content
    assert replies["merge-search"].status == "success"
    assert len(searches) == 1 and session.phase == "merge"
    assert "search_documents" not in model.bindings[0]
    assert "search_documents" in model.bindings[1]
    assert session.model_calls == len(model.calls) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_heading", [
    "### 1. 期限要求",
    "### 候选 1 期限要求",
    "### 候选 1\n期限要求",
])
async def test_candidate_format_feedback_repairs_observed_variants_in_one_edit(
    temp_settings, bad_heading,
):
    session = candidate_session(temp_settings)
    tool = next(t for t in build_rule_tools(session) if t.name == "rule_candidates")
    invalid = CANDIDATES.replace("### 候选 1", bad_heading)
    feedback = await tool.ainvoke({
        "action": "edit", "edits": [{"old_text": CANDIDATE_TEMPLATE, "new_text": invalid}],
    })
    assert "结构待修正" in feedback and session.candidate_draft == invalid
    assert "不另起名称行" in feedback or "不能另起名称行" in feedback
    example = re.search(r"```markdown\n(.*?)\n```", feedback, re.S)[1]
    assert example == CANDIDATE_ITEM_TEMPLATE
    with pytest.raises(ValueError, match="正确条目结构"):
        await tool.ainvoke({"action": "finish"})
    assert session.phase == "extract"
    # 按返回的结构仅修标题，不必read、重写业务正文或再猜一轮格式。
    result = await tool.ainvoke({
        "action": "edit",
        "edits": [{"old_text": bad_heading, "new_text": example.splitlines()[0]}],
    })
    assert "结构校验通过，候选1项" in result
    assert session.candidate_draft == CANDIDATES
    await tool.ainvoke({"action": "finish"})
    assert session.phase == "merge"


def test_candidate_duplicate_number_has_specific_feedback():
    duplicate = CANDIDATES + "\n" + CANDIDATES.split("## 候选清单\n\n", 1)[1]
    with pytest.raises(ValueError, match="候选编号重复：### 候选 1"):
        validate_candidates(duplicate)


def make_report(tmp_path, count=1):
    ctx = audit_context()
    if count > 1:
        ctx.rule_set.rules.extend(ctx.rule_set.rules[0].model_copy(update={"rule_id": f"rule-{i}"}) for i in range(2, count + 1))
    report = ReportDocument(tmp_path / "draft.md", ctx.rule_set, SourceLedger(ctx.parsed_documents), version=3)
    report.start_batch(1)
    return ctx, report


def progress_block(key, status="未完成"):
    return f"### {key}\n\n核验状态：{status}\n\n进度说明：已定位必要记录，尚需核对相邻正文。"


@pytest.mark.asyncio
async def test_save_gate_partial_incomplete_read_failure_and_old_legal_block(tmp_path):
    ctx, report = make_report(tmp_path, 2)
    saves = []
    policy = ReportSavePolicy(report, lambda: saves.append(list(policy.pending)))
    policy.pending = [{"batch_index": 1, "tool_round": 1, "tool_call_id": "q", "rule_ids": ["rule-1", "rule-2"]}]
    policy.begin_batch(2)
    await report.apply("read", None, None, "read")
    assert policy.pending
    with pytest.raises(ValueError):
        await report.apply("edit", None, [ReportEdit(old_text="missing", new_text="x")], "bad")
    text = report.read().replace("### rule-1\n\n待填写。", progress_block("rule-1"))
    await report.apply("write", text, None, "save")
    assert policy.pending[0]["rule_ids"] == ["rule-2"]
    assert "未完成" in report.progress()
    with pytest.raises(ValueError, match="报告未完成"):
        report.finish()
    # 已完成却只有内部字段仍不合格；无来源本身不阻止完成。
    invalid = text.replace("### rule-2\n\n待填写。", progress_block("rule-2", "已完成"))
    await report.apply("write", invalid, None, "bad-block")
    assert policy.pending
    valid = filled_report(ctx.rule_set, version=3)
    await report.apply("write", valid, None, "final")
    assert not policy.pending and report.finish()
    assert "核验状态" not in report.finish().model_dump_json()
    # 首版明确接受：关联块旧内容合法，后续合法写入可以解除；不做diff。
    policy.pending = [{"batch_index": 1, "tool_round": 2, "tool_call_id": "q2", "rule_ids": ["rule-1"]}]
    policy.begin_batch(3)
    await report.apply("write", valid, None, "same-text")
    assert not policy.pending


@pytest.mark.asyncio
async def test_pending_release_checkpoint_failure_and_invalid_source(tmp_path):
    ctx, report = make_report(tmp_path)
    def fail():
        raise OSError("disk")
    pending = [{"batch_index": 1, "tool_round": 1, "tool_call_id": "q", "rule_ids": ["rule-1"]}]
    policy = ReportSavePolicy(report, fail, pending=pending)
    policy.begin_batch(2)
    with pytest.raises(OSError):
        await report.apply("write", filled_report(ctx.rule_set, version=3), None, "save")
    assert policy.pending == pending
    policy.checkpoint = lambda: None
    prefix = report.read().split("### rule-1")[0]
    await report.apply("write", prefix + progress_block("rule-1") + "\n资料片段：fake", None, "invalid-source")
    assert policy.pending and "引用未实际读取" in report.progress()


@pytest.mark.asyncio
async def test_main_policy_multiple_same_tool_binding_and_adapter_isolation(tmp_path, temp_settings):
    ctx, report = make_report(tmp_path)
    called, snapshots = [], []
    async def run_python(code: str):
        called.append(code)
        if code == "error":
            raise ValueError("mock execution error")
        return "success"
    original = StructuredTool.from_function(coroutine=run_python, description="test")
    policy = ReportSavePolicy(report, lambda: snapshots.append(list(policy.pending)))
    tools = policy.adapt_tools([original, report_tool(report)])
    assert "rule_ids" not in convert_to_openai_tool(original)["function"]["parameters"]["properties"]
    assert "rule_ids" in convert_to_openai_tool(tools[0])["function"]["parameters"]["required"]
    text = filled_report(ctx.rule_set, version=3)
    calls = [tool_response("run_python", {"code": value, "rule_ids": ["rule-1"]}, f"q{i}").tool_calls[0] for i, value in enumerate(["ok", "error"])]
    # 同轮先保存+两次非报告调用仍建立新待保存；下轮未授权工具被拒绝但read正常返回。
    model = SequenceModel([
        AIMessage(content="", tool_calls=[tool_response("report_document", {"action": "write", "content": text, "edits": None}, "pre").tool_calls[0], *calls]),
        AIMessage(content="", tool_calls=[tool_response("run_python", {"code": "hidden", "rule_ids": ["rule-1"]}, "denied").tool_calls[0], tool_response("report_document", {"action": "read", "content": None, "edits": None}, "read").tool_calls[0]]),
        tool_response("report_document", {"action": "write", "content": text, "edits": None}, "saved"),
        "处理结束",
    ])
    await run_session(model=model, tools=tools, settings=temp_settings.bid_chat_model, max_steps=5,
                      role="bid_agent", limiter=asyncio.Semaphore(1), trace_context={"enabled": False},
                      build_messages=lambda _: [SystemMessage(content="test"), HumanMessage(content=report.read())],
                      report=report, policy=policy)
    assert called == ["ok", "error"]
    assert model.bindings[1:3] == [["report_document"], ["report_document"]]
    assert "run_python" in model.bindings[3] and not policy.pending
    assert len(snapshots[1]) == 2
    returns = [m for m in model.calls[2][0] if m.type == "tool"]
    assert next(m for m in returns if m.tool_call_id == "denied").status == "error"
    assert "待保存规则：rule-1" in next(m for m in returns if m.tool_call_id == "read").content


@pytest.mark.parametrize("role,notice", [("bid_agent", True), ("evidence_subagent", False)])
def test_only_main_extra_reminder_once(temp_settings, role, notice):
    state = {"messages": [SystemMessage(content="test"), HumanMessage(content="original")], "model_calls": 12}
    context = prepare_request(state, tools=[], max_steps=15, settings=temp_settings.bid_chat_model, role=role)
    assert len(context.messages) == 2 + notice
    again = prepare_request({**state, "messages": list(context.messages), "prepared_step": 13}, tools=[], max_steps=15, settings=temp_settings.bid_chat_model, role=role)
    assert again.messages == context.messages


@pytest.mark.parametrize("data", [{}, {"querylist": []}, {"chunklist": []}, {"chunklist": [{"chunk_id": "c", "file_id": "e-1", "before": -1, "after": 0}]}, {"chunklist": [{"chunk_id": "c", "file_id": "e-1", "before": True, "after": 0}]}])
def test_search_modes_invalid(data):
    with pytest.raises(ValidationError):
        SearchDocumentsInput.model_validate(data)


@pytest.mark.asyncio
async def test_neighbors_order_center_overlap_boundaries_permissions_and_ledger(temp_settings):
    ctx = audit_context()
    parsed = ctx.parsed_documents["e-1"]
    chunks = [IndexChunk(chunk_id=key, file_id="e-1", bidder_id="bidder-1", data_role="evidence_material", file_name="e-1.md", source_path="e-1.md", section="记录", content=f"完整正文{i}", parse_status="parsed", parse_version=parsed.markdown_path) for i, key in enumerate(["z", "a", "q", "b", "last"])]
    index = LoadedIndex(IndexManifest(dimension=2, chunk_count=5, embedding_model="mock", embedding_fingerprint="mock"), chunks, np.zeros((5, 2)), {})
    scope = scope_for(parsed.document)
    service = RetrievalService(None, None, temp_settings, indexes={scope.scope_id: index})
    ledger, files = SourceLedger(ctx.parsed_documents), []
    tool = CommonTool(parsed_documents={"e-1": parsed}, settings=temp_settings, retrieval=service, file_filters={"e-1": scope}, on_source_read=files.append, on_chunk_read=ledger.chunk_read).search_tool()
    def group(key, before=0, after=0, file="e-1"):
        return dict(chunk_id=key, before=before, after=after, file_id=file)
    output = await tool.ainvoke({"chunklist": [group("a", 9, 1), group("q", 1), group("last", 0, 20), group("z", file="e-2"), group("fake")]})
    assert re.findall(r"### 片段 (\S+)", output) == ["z", "a", "q", "last"]
    assert "当前索引开头" in output and "当前索引末尾" in output and "部分完成" in output
    assert "\n\n---\n\n" in output and "角色：" not in output
    assert set(ledger.sources) == {"z", "a", "q", "last"} and files == ["e-1"] * 4
    again = await tool.ainvoke({"chunklist": [group("a")]})
    assert "### 片段 a" in again
    chunks[0].parse_version = "old"
    error = await tool.ainvoke({"chunklist": [group("a")]})
    assert "版本" in error and "### 片段" not in error
    assert len(files) == 5


@pytest.mark.asyncio
async def test_neighbors_preserve_tables_images_and_first_file_order(temp_settings):
    docs = {key: parsed_file(key, "evidence_material") for key in ("e-1", "e-2")}
    path = "e-1/images/1.png"
    docs["e-1"].sections[0].blocks[0].images = [
        ParsedImage(path=path, mime_type="image/png", width=10, height=10),
    ]
    table = "| 原始列一 | 原始列二 |\n|---|---|\n| 多行值 | 123 |"
    chunks = [
        IndexChunk(
            chunk_id=f"{key}-chunk", file_id=key, bidder_id="bidder-1",
            data_role="evidence_material", file_name=f"{key}.md", source_path=f"{key}.md",
            section="记录", content=table, block_kind="table", parse_status="parsed",
            parse_version=doc.markdown_path, image_paths=[path] if key == "e-1" else [],
        )
        for key, doc in docs.items()
    ]
    scope = scope_for(docs["e-1"].document)
    index = LoadedIndex(
        IndexManifest(dimension=2, chunk_count=2, embedding_model="mock", embedding_fingerprint="mock"),
        chunks, np.zeros((2, 2)), {},
    )
    ledger = SourceLedger(docs)
    tool = CommonTool(
        parsed_documents=docs, settings=temp_settings, query_filter=scope,
        retrieval=RetrievalService(None, None, temp_settings, indexes={scope.scope_id: index}),
        on_chunk_read=ledger.chunk_read,
    ).search_tool()
    groups = [dict(chunk_id=f"{key}-chunk", before=0, after=0, file_id=key) for key in ("e-2", "e-1")]
    result = await tool.ainvoke({"chunklist": groups})
    assert re.findall(r"### 片段 (\S+)", result) == ["e-2-chunk", "e-1-chunk"]
    assert result.count(table) == 2 and path in result
    assert ledger.sources["e-1-chunk"].quote == table
    assert path not in ledger.sources  # 返回图片入口不等于成功看过图片。
    chunks[0].image_paths = ["e-1/images/not-registered.png"]
    ledger.sources.clear()
    result = await tool.ainvoke({"chunklist": groups})
    assert "部分完成" in result and "图片未登记" in result
    assert list(ledger.sources) == ["e-2-chunk"]


@pytest.mark.asyncio
async def test_neighbor_history_read_satisfies_existing_rule_source_check(temp_settings):
    ctx = audit_context()
    history = parsed_file("tender-2", "rule_source")
    ctx.rule_set.source_files.append(RuleSourceFile(
        file_id="tender-2", file_name="tender-2.md", source_path="tender-2.md",
    ))
    current = ctx.parsed_documents["tender-1"]
    scope = scope_for(current.document)
    chunk = IndexChunk(
        chunk_id="historical-fragment", file_id="tender-2", project_id="project-1",
        data_role="rule_source", file_name="tender-2.md", source_path="tender-2.md",
        section="补充说明", content="期限90日。", parse_status="parsed", parse_version=history.markdown_path,
    )
    index = LoadedIndex(
        IndexManifest(dimension=2, chunk_count=1, embedding_model="mock", embedding_fingerprint="mock"),
        [chunk], np.zeros((1, 2)), {},
    )
    session = RuleSession(
        rule_set=ctx.rule_set, current=current, history={"tender-2": history},
        persist=AsyncMock(), rule_limit=100,
        retrieval=RetrievalService(None, None, temp_settings, indexes={scope.scope_id: index}),
    )
    request = SubmitRuleDeltaInput.model_validate({
        "operations": [{
            "action": "replace", "rule_id": "rule-1", "category": "technical",
            "requirement": "分辨率至少640×480，期限90日。", "priority": "critical",
            "verification": "evidence_check", "reason": "两份文件共同规定。",
            "source_file_ids": ["tender-1", "tender-2"],
        }], "project_profile": None, "return_view": "all",
    })
    with pytest.raises(ValueError, match="历史来源须先实际回查"):
        await session.submit(request, "replace")
    search = next(tool for tool in build_rule_tools(session) if tool.name == "search_documents")
    await search.ainvoke({"chunklist": [{"chunk_id": "historical-fragment", "file_id": "tender-2", "before": 0, "after": 0}]})
    await session.submit(request, "replace")
    assert {source.file_id for source in session.rule_set.rules[0].sources} == {"tender-1", "tender-2"}


@pytest.mark.asyncio
async def test_r_graph_phase_snapshot_and_next_file_reset(temp_settings, monkeypatch):
    from tests.unit.rules.test_rule_agent import parsed_file as rule_file, tool_call, submit_args, add_operation
    from app.rules.extractor import extract_rule_set
    from app.storage.database import initialize_database
    monkeypatch.setattr("app.storage.database.database_path", lambda: temp_settings.storage.database_path)
    initialize_database(temp_settings.storage.database_path)
    replies = []
    for file in range(2):
        replies.extend([
            AIMessage(content="", tool_calls=[tool_call("rule_candidates", "edit", {"action": "edit", "edits": [{"old_text": CANDIDATE_TEMPLATE, "new_text": CANDIDATES}]}), tool_call("rule_candidates", "finish", {"action": "finish"}), tool_call("submit_rule_delta", "hidden", submit_args([add_operation()], view="all"))]),
            tool_response("submit_rule_delta", submit_args([add_operation()], view="all"), "merge"),
            "已合并",
        ])
    model = SequenceModel(replies)
    progress = []
    result = await extract_rule_set("project-1", [rule_file(1), rule_file(2)], model, retrieval_service=SimpleNamespace(settings=temp_settings), settings=temp_settings, progress_callback=progress.append)
    assert len(result.rules) == 2
    assert "submit_rule_delta" not in model.bindings[0] and "rule_candidates" not in model.bindings[1]
    assert "rule_candidates" in model.bindings[3]
    assert "search_documents" not in model.bindings[0] and "search_documents" not in model.bindings[3]
    assert "search_documents" in model.bindings[1]
    assert all(p.candidate_draft == CANDIDATE_TEMPLATE for p in progress if p.batch_index == 0)
    tools = [m for m in model.calls[1][0] if m.type == "tool"]
    assert next(m for m in tools if m.tool_call_id == "hidden").status == "error"
    # 第二个文件提取输入不泄露已存rule-1；finish返回才交接旧规则。
    assert "### rule-1" not in "\n".join(m.content for m in model.calls[3][0])


@pytest.mark.asyncio
async def test_r_finish_order_failed_edit_and_no_extra_merge_round(temp_settings):
    from app.graph.rule_graph import build_rule_graph
    from app.rules.extractor import _check_batch_completion
    from app.rules.context import rule_system

    s = candidate_session(temp_settings)
    s.candidate_draft = CANDIDATES
    edit = {"action": "edit", "edits": [{"old_text": "90日", "new_text": "90日"}]}
    def call(args, id):
        return tool_response("rule_candidates", args, id).tool_calls[0]
    model = SequenceModel([
        AIMessage(content="", tool_calls=[call({"action": "finish"}, "too-early"), call(edit, "edit")]),
        AIMessage(content="", tool_calls=[call({"action": "edit", "edits": [{"old_text": "absent", "new_text": "x"}]}, "bad-edit"), call({"action": "finish"}, "after-error")]),
        AIMessage(content="", tool_calls=[call(edit, "repair"), call({"action": "finish"}, "finish")]),
        "已结束",
    ])
    graph = build_rule_graph(model=model, tools=build_rule_tools(s), session=s, max_steps=4,
                             controller=make_controller(temp_settings.rule_extraction_model))
    final = await graph.ainvoke({"messages": [SystemMessage(content=rule_system(s)), HumanMessage(content="原文")], "model_calls": 0})
    replies = {m.tool_call_id: m for m in final["messages"] if m.type == "tool"}
    assert "最后一个候选调用" in replies["too-early"].content
    assert replies["bad-edit"].status == replies["after-error"].status == "error"
    assert s.phase == "merge" and not s.candidate_error and len(model.calls) == 4
    assert not s.merge_tool_round_seen
    with pytest.raises(RuntimeError, match="已无合并工具轮"):
        _check_batch_completion(final, s)
    # 真正零候选可在最后工具轮交接；不凭聊天中的“结束”切阶段。
    s.candidate_draft = "# 当前文件候选草稿\n\n候选简介：只有背景信息。\n\n## 候选清单\n\n无候选。"
    _check_batch_completion(final, s)
    s.phase = "extract"
    with pytest.raises(RuntimeError, match="尚未成功finish"):
        _check_batch_completion(final, s)


@pytest.mark.asyncio
async def test_r_same_file_batches_and_rebuild_preserve_candidate(temp_settings, monkeypatch):
    from app.rules import extractor
    from app.storage.database import initialize_database
    from tests.unit.rules.test_rule_agent import parsed_file as rule_file, add_operation, submit_args

    monkeypatch.setattr("app.storage.database.database_path", lambda: temp_settings.storage.database_path)
    initialize_database(temp_settings.storage.database_path)
    original_plan = extractor.plan_document_input
    def one_unit(**kwargs):
        return original_plan(**{**kwargs, "units": kwargs["units"][:kwargs["cursor"] + 1]})
    monkeypatch.setattr(extractor, "plan_document_input", one_unit)
    parsed = rule_file(1)
    block = parsed.sections[0].blocks[0]
    parsed.sections[0].blocks.append(block.model_copy(update={"source_block_index": 1, "content": "第二批正文"}))
    model = SequenceModel([
        candidate_response("90日。"),
        tool_response("submit_rule_delta", submit_args([add_operation()], view="all")),
        "第一批合并完成",
        tool_response("rule_candidates", {"action": "finish"}),
        "后续正文未新增要求",
    ])
    progress = []
    await extractor.extract_rule_set("project-1", [parsed], model, settings=temp_settings,
                                    retrieval_service=SimpleNamespace(settings=temp_settings), progress_callback=progress.append)
    assert progress[-1].batch_index == 2 and progress[-1].next_unit_index == 2
    assert "要求：90日。" in model.calls[3][0][1].content
    assert "### rule-1" not in model.calls[3][0][1].content
    assert "search_documents" not in model.bindings[3]
    # 压缩重建使用当前稿、当前阶段，不重置调用轮数；提取仍不展示旧库。
    s = candidate_session(temp_settings)
    s.candidate_draft, s.batch_index = CANDIDATES, 2
    for phase in ("extract", "merge"):
        s.phase = phase
        rebuilt = extractor._batch_context(session=s, units=extractor.build_document_units(s.current), start=0, end=1,
                                           tools=build_rule_tools(s), settings=temp_settings, model_calls=3, handoff="真实历史交接")
        assert CANDIDATES in rebuilt.messages[1].content
        assert ("OLD_PROFILE_SENTINEL" in rebuilt.messages[1].content) == (phase == "merge")
        assert "第 4/8 轮" in rebuilt.messages[1].content


@pytest.mark.asyncio
async def test_r_concurrent_current_drafts_are_isolated(temp_settings):
    sessions = [candidate_session(temp_settings) for _ in range(2)]
    await asyncio.gather(*(s.candidates("edit", [CandidateEdit(old_text=CANDIDATE_TEMPLATE, new_text=CANDIDATES.replace("90日", f"{i}日"))]) for i, s in enumerate(sessions, 1)))
    assert "1日" in sessions[0].candidate_draft and "2日" in sessions[1].candidate_draft
    await sessions[0].candidates("finish")
    assert sessions[1].phase == "extract"


@pytest.mark.asyncio
async def test_h_rejects_bad_association_without_executing(tmp_path):
    _, report = make_report(tmp_path)
    invoked = []
    async def run_python(code: str):
        invoked.append(code)
        return "success"
    policy = ReportSavePolicy(report, lambda: None)
    tool = policy.adapt_tools([StructuredTool.from_function(coroutine=run_python, description="test")])[0]
    for ids in (None, [], ["unknown"], ["rule-1", "rule-1"]):
        args = {"code": "print(1)", **({"rule_ids": ids} if ids is not None else {})}
        with pytest.raises((ValueError, ValidationError)):
            await tool.ainvoke({"type": "tool_call", "name": tool.name, "id": "bad", "args": args})
    assert not invoked and not policy.executed and not policy.pending


@pytest.mark.asyncio
async def test_h_structure_and_receipt_replay_do_not_clear_pending(tmp_path):
    ctx, report = make_report(tmp_path)
    pending = [{"batch_index": 1, "tool_round": 1, "tool_call_id": "q", "rule_ids": ["rule-1"]}]
    policy = ReportSavePolicy(report, lambda: None)
    policy.begin_batch(1)
    valid = filled_report(ctx.rule_set, version=3)
    await report.apply("write", valid, None, "old-save")
    policy.pending = list(pending)
    policy.begin_batch(2)
    replay = await report.apply("write", valid, None, "old-save")
    assert policy.pending and "待保存规则：rule-1" in replay and "未再次写入" in replay
    await report.apply("write", valid + "\n### unknown-rule\n正文", None, "bad-identity")
    assert policy.pending
    await report.apply("write", valid, None, "good")
    assert not policy.pending


@pytest.mark.asyncio
async def test_h_last_tool_result_is_not_silently_discarded(tmp_path, temp_settings):
    _, report = make_report(tmp_path)
    checkpoints = []
    async def search_documents(query: str):
        return "没有命中"
    policy = ReportSavePolicy(report, lambda: checkpoints.append(list(policy.pending)))
    tools = policy.adapt_tools([StructuredTool.from_function(coroutine=search_documents, description="test"), report_tool(report)])
    model = SequenceModel([
        tool_response("report_document", {"action": "read", "content": None, "edits": None}),
        tool_response("search_documents", {"query": "具体事实", "rule_ids": ["rule-1"]}, "last-query"),
        "结束",
    ])
    with pytest.raises(ValueError, match="未保存进度"):
        await run_session(model=model, tools=tools, settings=temp_settings.bid_chat_model, max_steps=3,
                          role="bid_agent", limiter=asyncio.Semaphore(1), trace_context={"enabled": False},
                          build_messages=lambda _: [SystemMessage(content="test"), HumanMessage(content=report.read())], report=report, policy=policy)
    assert len(model.calls) == 3 and checkpoints[-1][0]["tool_call_id"] == "last-query"
    # 重建消息/策略不丢门禁；不靠新增Human重放业务结果。
    restored = ReportSavePolicy(report, lambda: None, pending=checkpoints[-1])
    rebuilt = prepare_request({"messages": [SystemMessage(content="test"), HumanMessage(content=report.read())], "model_calls": 1},
                              tools=tools, max_steps=15, settings=temp_settings.bid_chat_model, role="bid_agent", policy=restored)
    assert [item["function"]["name"] for item in rebuilt.bindings["tools"]] == ["report_document"]


@pytest.mark.parametrize("before", [1.5, "1"])
def test_l_strict_spans_and_modes(before):
    group = {"chunk_id": "actual", "file_id": "e-1", "before": before, "after": 0}
    with pytest.raises(ValidationError):
        SearchDocumentsInput.model_validate({"chunklist": [group]})
    group["before"] = 0
    with pytest.raises(ValidationError):
        SearchDocumentsInput.model_validate({"chunklist": [group], "querylist": [{"queries": ["q"], "file_ids": None}]})
    assert SearchDocumentsInput.model_validate({"chunklist": [group], "querylist": None})
    group.pop("file_id")
    with pytest.raises(ValidationError):
        SearchDocumentsInput.model_validate({"chunklist": [group]})

