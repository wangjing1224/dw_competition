"""真实图/工具闭环配合脚本模型，不连接外部模型。"""

import json
from io import BytesIO
from math import ceil
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.tools import StructuredTool
from langsmith import tracing_context
from langsmith.run_trees import RunTree
from PIL import Image

from app.llm.context_compaction import InputBudgetError
from app.llm.token_counter import TokenCounter
from app.models.documents import (
    DocumentRef,
    ParsedBlock,
    ParsedDocument,
    ParsedSection,
    SourceRef,
)
from app.models.rules import (
    RuleExtractionProgress,
    RuleSet,
    RuleSourceFile,
    SubmitRuleDeltaInput,
)
from app.parsers.assets import AssetStore, file_asset_root
from app.retrieval.models import SearchResult
from app.rules.extractor import extract_rule_set
from app.rules.local_trace import LocalTrace, current_trace
from app.storage.database import initialize_database
from app.tools.common_tools import CommonTool
from app.tools.rule_tools import RuleSession, build_rule_tools


def parsed_file(number):
    key = f"file-{number}"
    return ParsedDocument(
        schema_version=2,
        markdown_path=f"{key}/versions/v1/document.md",
        document=DocumentRef(
            file_id=key,
            file_name=f"{number}.md",
            source_path=f"目录/{number}.md",
            project_id="project-1",
            data_role="rule_source",
        ),
        parse_status="parsed",
        sections=[
            ParsedSection(
                section_id=f"s-{number}",
                title="规范",
                blocks=[
                    ParsedBlock(
                        kind="text",
                        source_block_index=0,
                        content=f"文件{number}：期限及起算时间。",
                    )
                ],
            )
        ],
    )


def submit_args(operations=None, profile=None, view=None):
    return dict(operations=operations, project_profile=profile, return_view=view)


def add_operation(**overrides):
    return dict(
        action="add",
        category="commercial",
        requirement="期限90日。",
        priority="important",
        verification="bid_only",
        reason="file-1规定了90日。",
        **overrides,
    )


def tool_call(name, call_id, args):
    return dict(name=name, id=call_id, args=args, type="tool_call")


class ScriptModel:
    def __init__(self, replies):
        self.replies = iter(replies)
        self.calls = []
        self.options = []

    def bind_tools(self, tools):
        async def invoke(messages, **kwargs):
            self.options.append(kwargs)
            return await self.invoke(messages, [tool.name for tool in tools])

        return SimpleNamespace(ainvoke=invoke)

    async def ainvoke(self, messages, **kwargs):
        self.options.append(kwargs)
        return await self.invoke(messages, [])

    async def invoke(self, messages, tools):
        # 验证每次模型调用前所有上一轮 tool_call 都已回应，提醒不能插到中间。
        pending = set()
        for message in messages:
            if isinstance(message, ToolMessage):
                assert message.tool_call_id in pending
                pending.remove(message.tool_call_id)
            else:
                assert not pending
                pending.update(
                    call["id"]
                    for call in [
                        *getattr(message, "tool_calls", []),
                        *getattr(message, "invalid_tool_calls", []),
                    ]
                )
        assert not pending
        self.calls.append((list(messages), tools))
        return next(self.replies)


@pytest.fixture
def env(monkeypatch, temp_settings):
    # 本文件原有用例隔离验证submit/合并/预算：预置已交接阶段，不虚增提取轮。
    # Task37完整两阶段及生命周期在test_task37.py使用真实extract入口验证。
    import app.rules.extractor as extractor
    batch_context = extractor._batch_context

    def merge_context(**kwargs):
        session = kwargs["session"]
        session.phase = "merge"
        session.candidate_draft = "# 当前文件候选草稿\n\n候选简介：合并阶段回归测试起点。\n\n## 候选清单\n\n无候选。"
        return batch_context(**kwargs)

    monkeypatch.setattr(extractor, "_batch_context", merge_context)
    monkeypatch.setattr(
        "app.storage.database.database_path",
        lambda: temp_settings.storage.database_path,
    )
    initialize_database(temp_settings.storage.database_path)
    return temp_settings.model_copy(update={"rule_agent_max_steps": 3})


async def extract(env, model, files, callback=None):
    return await extract_rule_set(
        "project-1",
        files,
        model,
        retrieval_service=SimpleNamespace(settings=env),
        settings=env,
        progress_callback=callback,
    )


@pytest.mark.asyncio
async def test_invalid_rule_json_then_corrected_submit(env):
    model = ScriptModel(
        [
            AIMessage(
                content="",
                invalid_tool_calls=[
                    {
                        "name": "submit_rule_delta",
                        "id": "bad-json",
                        "args": '{"operations":',
                    }
                ],
            ),
            AIMessage(
                content="",
                tool_calls=[
                    tool_call(
                        "submit_rule_delta",
                        "fixed",
                        submit_args([add_operation()], view="all"),
                    )
                ],
            ),
            AIMessage(content="当前范围已保存"),
        ]
    )
    result = await extract(env, model, [parsed_file(1)])
    assert len(result.rules) == 1 and len(model.calls) == 3
    assert model.calls[0][1].count("run_python") == 1
    feedback = next(m for m in model.calls[1][0] if m.type == "tool")
    assert feedback.status == "error" and feedback.tool_call_id == "bad-json"
    assert "JSON 解析失败" in feedback.content


@pytest.mark.asyncio
@pytest.mark.parametrize("limit, warned", [(1, True), (2, False), (10_000, False)])
async def test_rule_limit_hook_warns_but_does_not_change_rules(
    env, caplog, limit, warned
):
    settings = env.model_copy(update={"rule_limit": limit})
    model = ScriptModel(
        [
            AIMessage(
                content="",
                tool_calls=[
                    tool_call(
                        "submit_rule_delta",
                        "write",
                        submit_args(
                            [
                                add_operation(),
                                {
                                    **add_operation(),
                                    "requirement": "报价不得超过最高限价。",
                                },
                            ],
                            view="all",
                        ),
                    )
                ],
            ),
            AIMessage(content="处理完毕"),
        ]
    )
    result = await extract(settings, model, [parsed_file(1)])
    assert len(result.rules) == 2
    path = settings.storage.data_root / "parsed/rules/project-1.progress.json"
    progress = RuleExtractionProgress.model_validate_json(path.read_text())
    assert len(progress.rule_set.rules) == 2 and progress.error is None
    warnings = [
        record.getMessage()
        for record in caplog.records
        if record.name == "app.rules.policies"
    ]
    assert len(warnings) == int(warned)
    if warned:
        assert "project=project-1 count=2 limit=1" in warnings[0]


@pytest.mark.asyncio
async def test_rule_limit_hook_skips_read_replay_and_invalid_batch(env, caplog):
    parsed = parsed_file(1)
    writer = AsyncMock()
    session = RuleSession(
        RuleSet(
            project_id="project-1",
            source_files=[RuleSourceFile.from_document(parsed.document)],
        ),
        parsed,
        {},
        writer,
        SimpleNamespace(settings=env),
        rule_limit=1,
    )
    request = SubmitRuleDeltaInput(
        **submit_args(
            [
                add_operation(),
                {**add_operation(), "requirement": "报价不得超过最高限价。"},
            ],
            view="all",
        )
    )
    await session.submit(request, "write")
    assert "count=2 limit=1" in caplog.text
    caplog.clear()

    await session.submit(SubmitRuleDeltaInput(**submit_args()), "read")
    await session.submit(request, "write")
    invalid = SubmitRuleDeltaInput(
        **submit_args(
            [
                add_operation(),
                {"action": "merge", "rule_id": "missing", "reason": ""},
            ],
            view="all",
        )
    )
    with pytest.raises(ValueError, match="不存在"):
        await session.submit(invalid, "invalid")
    assert not [
        record for record in caplog.records if record.name == "app.rules.policies"
    ]
    assert len(session.rule_set.rules) == 2 and writer.await_count == 1


@pytest.mark.asyncio
async def test_serial_files_have_fresh_messages_and_latest_rules(env):
    first = tool_call(
        "submit_rule_delta",
        "write-1",
        submit_args([add_operation()], "设备采购项目", "changed"),
    )
    read = tool_call("submit_rule_delta", "read-1", submit_args())
    second = tool_call(
        "submit_rule_delta",
        "write-2",
        submit_args(
            [
                dict(
                    action="merge",
                    rule_id="rule-1",
                    requirement="从截止日起算。",
                    reason="期限来自file-1；起算来自file-2。",
                )
            ],
            view="changed",
        ),
    )
    model = ScriptModel(
        [
            AIMessage(content="", tool_calls=[first, read]),
            AIMessage(content="处理完毕"),
            AIMessage(content="", tool_calls=[second]),
            AIMessage(content="处理完毕"),
        ]
    )
    progress = []
    result = await extract(
        env,
        model,
        [parsed_file(1), parsed_file(2)],
        lambda value: progress.append(value),
    )
    assert result.rules[0].requirement == "期限90日。\n从截止日起算。"
    assert [source.file_id for source in result.rules[0].sources] == [
        "file-1",
        "file-2",
    ]
    initial_second = model.calls[2][0]
    assert len(initial_second) == 2 and "rule-1" in initial_second[1].content
    assert "文件1：期限" not in initial_second[1].content
    tools = [m for m in model.calls[1][0] if isinstance(m, ToolMessage)]
    assert len(tools) == 2 and all("rule-1" in m.content for m in tools)
    assert progress[-1].completed_file_ids == ["file-1", "file-2"]
    assert any(p.rule_set.rules and not p.completed_file_ids for p in progress)
    assert progress[-1].rule_set.rules[0].provenance == "ai_generated"


@pytest.mark.asyncio
@pytest.mark.parametrize("steps", [3, 8, 12, 15])
async def test_last_round_is_unbound_with_all_previous_calls_closed(env, steps):
    env = env.model_copy(update={"rule_agent_max_steps": steps})
    model = ScriptModel(
        [
            *[
                AIMessage(
                    content="",
                    tool_calls=[
                        tool_call("submit_rule_delta", f"r-{n}", submit_args())
                    ],
                )
                for n in range(1, steps)
            ],
            AIMessage(content="处理完毕"),
        ]
    )
    assert not (await extract(env, model, [parsed_file(1)])).rules
    assert [bool(tools) for _, tools in model.calls] == [True] * (steps - 1) + [False]
    assert model.calls[-2][0][-1].type == "human"
    assert str(steps - 1) in model.calls[-2][0][-1].content
    assert model.calls[-1][0][-1].type == "human"
    assert str(steps) in model.calls[-1][0][-1].content
    assert model.calls[0][0][0] == model.calls[-1][0][0]
    assert len(model.calls) == steps


@pytest.mark.asyncio
async def test_tool_validation_error_is_closed_and_can_be_corrected(env):
    model = ScriptModel(
        [
            AIMessage(
                content="", tool_calls=[tool_call("submit_rule_delta", "bad", {})]
            ),
            AIMessage(
                content="",
                tool_calls=[
                    tool_call(
                        "submit_rule_delta",
                        "fixed",
                        submit_args([add_operation()], view="all"),
                    )
                ],
            ),
            AIMessage(content="处理完毕"),
        ]
    )
    result = await extract(env, model, [parsed_file(1)])
    assert len(result.rules) == 1
    error = next(m for m in model.calls[1][0] if isinstance(m, ToolMessage))
    assert error.status == "error" and "提交未正常完成" in error.content


@pytest.mark.asyncio
@pytest.mark.parametrize("corrected", [True, False])
async def test_null_profile_text_preserves_saved_data_and_returns_fixable_error(env, corrected):
    """回放先存简介、后误传字符串null；拒绝整批且不在工具报错时直接终止。"""
    original_profile = "项目为数字化加工服务，范围与版本以当前招标文件为准。"
    new_operation = {**add_operation(), "requirement": "交付物须附目录。"}
    replies = [
        AIMessage(content="", tool_calls=[
            tool_call("submit_rule_delta", "initial", submit_args(
                [add_operation()], profile=original_profile, view="changed",
            )),
        ]),
        AIMessage(content="", tool_calls=[
            tool_call("submit_rule_delta", "bad-profile", submit_args(
                [new_operation], profile="null", view="changed",
            )),
        ]),
    ]
    if corrected:
        replies.append(AIMessage(content="", tool_calls=[
            tool_call("submit_rule_delta", "fixed-profile", submit_args(
                [new_operation], profile=None, view="changed",
            )),
        ]))
    replies.append(AIMessage(content="当前范围处理完毕"))
    settings = env.model_copy(update={"rule_agent_max_steps": len(replies)})
    model = ScriptModel(replies)
    if corrected:
        result = await extract(settings, model, [parsed_file(1)])
        assert result.project_profile == original_profile
        assert len(result.rules) == 2
    else:
        # 用尽本来配置的轮次仍未修正时沿用原失败语义，保留有效数据。
        with pytest.raises(RuntimeError, match="规则提交尚未成功"):
            await extract(settings, model, [parsed_file(1)])
    feedback = next(
        m for m in model.calls[2][0]
        if isinstance(m, ToolMessage) and m.tool_call_id == "bad-profile"
    )
    assert feedback.status == "error"
    assert 'project_profile 不能是字符串 "null"' in feedback.content
    assert "不修改简介请传 JSON null" in feedback.content
    assert "本次提交未写入" in feedback.content
    progress = RuleExtractionProgress.model_validate_json(
        (settings.storage.data_root / "parsed/rules/project-1.progress.json").read_text()
    )
    assert progress.rule_set.project_profile == original_profile
    assert len(progress.rule_set.rules) == (2 if corrected else 1)
    assert len(progress.commits) == (2 if corrected else 1)
    assert [r.rule_id for r in progress.rule_set.rules] == (
        ["rule-1", "rule-2"] if corrected else ["rule-1"]
    )
    assert bool(progress.error) is not corrected


@pytest.mark.asyncio
async def test_failed_last_tool_call_is_not_success_and_progress_retained(env):
    model = ScriptModel(
        [
            AIMessage(
                content="",
                tool_calls=[
                    tool_call(
                        "submit_rule_delta",
                        "good",
                        submit_args([add_operation()], view="all"),
                    )
                ],
            ),
            AIMessage(
                content="", tool_calls=[tool_call("submit_rule_delta", "bad", {})]
            ),
            AIMessage(content="处理完毕"),
        ]
    )
    with pytest.raises(RuntimeError, match="提交尚未成功"):
        await extract(env, model, [parsed_file(1)])
    path = env.storage.data_root / "parsed/rules/project-1.progress.json"
    progress = RuleExtractionProgress.model_validate_json(path.read_text())
    assert len(progress.rule_set.rules) == 1 and progress.error
    assert not progress.completed_file_ids


@pytest.mark.asyncio
async def test_write_failure_does_not_publish_and_call_id_replay_is_idempotent(env):
    parsed = parsed_file(1)
    rules = RuleSet(
        project_id="project-1",
        source_files=[RuleSourceFile.from_document(parsed.document)],
    )
    writer = AsyncMock(side_effect=OSError("磁盘故障"))
    session = RuleSession(
        rules,
        parsed,
        {},
        writer,
        SimpleNamespace(settings=env),
        rule_limit=env.rule_limit,
    )
    request = SubmitRuleDeltaInput(**submit_args([add_operation()], "简介", "changed"))
    with pytest.raises(OSError):
        await session.submit(request, "write")
    assert not session.rule_set.rules and not session.rule_set.project_profile
    writer.side_effect = None
    view = await session.submit(request, "write")
    assert len(session.rule_set.rules) == 1
    assert await session.submit(request, "write") == view
    assert writer.await_count == 2
    assert len(session.commits) == 1
    assert "rule-2" in view  # 失败预留的编号不重复使用。


@pytest.mark.asyncio
async def test_submit_tool_normalizes_only_inactive_nulls_and_keeps_replay(env):
    parsed = parsed_file(1)
    writer = AsyncMock()
    session = RuleSession(
        RuleSet(project_id="project-1", source_files=[RuleSourceFile.from_document(parsed.document)]),
        parsed, {}, writer, SimpleNamespace(settings=env), rule_limit=env.rule_limit,
    )
    submit = next(t for t in build_rule_tools(session) if t.name == "submit_rule_delta")
    assert set(submit.tool_call_schema.model_json_schema()["properties"]) == {
        "operations", "project_profile", "return_view",
    }
    operations = [
        add_operation(rule_id=None, merged_rule_ids=None, source_file_ids=None),
        {"action": "merge", "rule_id": "rule-1", "requirement": "从截止日起算。",
         "reason": "期限及起算事件。", "merged_rule_ids": None, "source_file_ids": None},
        {**add_operation(), "action": "replace", "rule_id": "rule-1",
         "requirement": "自截止日起90日。", "source_file_ids": ["file-1"],
         "merged_rule_ids": None},
    ]
    for index, operation in enumerate(operations):
        call = tool_call("submit_rule_delta", f"save-{index}", submit_args([operation], view="changed"))
        result = await submit.ainvoke(call)
        assert result.status == "success" and "rule-1" in result.content
        minimal = {key: value for key, value in operation.items() if value is not None}
        replay = await submit.ainvoke({**call, "args": submit_args([minimal], view="changed")})
        assert replay.content == result.content
    assert writer.await_count == len(session.commits) == 3
    assert session.rule_set.rules[0].requirement == "自截止日起90日。"
    assert [s.file_id for s in session.rule_set.rules[0].sources] == ["file-1"]
    assert len(session.rule_set.rules) == 1


@pytest.mark.asyncio
async def test_common_image_tool_registered_scope_and_public_schema(env):
    parsed = parsed_file(1)
    image = BytesIO()
    Image.new("RGB", (40, 40), "white").save(image, format="PNG")
    store = AssetStore(file_asset_root(env.storage.data_root, "file-1"))
    asset = store.save_image(image.getvalue())
    parsed.sections[0].blocks.append(
        ParsedBlock(kind="image", source_block_index=1, images=[asset])
    )
    tool = CommonTool(parsed_documents={"file-1": parsed}, settings=env).tools()[0]
    assert set(tool.tool_call_schema.model_json_schema()["properties"]) == {"path"}
    response = await tool.ainvoke(
        tool_call("view_image", "image-1", {"path": asset.path})
    )
    assert response.tool_call_id == "image-1"
    assert response.content[1]["image_url"]["url"].startswith("data:image/png;base64,")
    for path in [
        "file-2/images/1.png",
        "file-1/images/999.png",
        "../secrets.png",
        "/tmp/pic.png",
    ]:
        with pytest.raises((ValueError, FileNotFoundError)):
            await tool.ainvoke(tool_call("view_image", "bad-path", {"path": path}))


@pytest.mark.asyncio
async def test_group_search_enforces_history_scope_and_version(env):
    old, current = parsed_file(1), parsed_file(2)
    retrieval = SimpleNamespace(
        settings=env,
        search=AsyncMock(
            return_value=[
                SearchResult(
                    chunk_id="chunk-1",
                    score=1,
                    project_id="project-1",
                    content="期限原文90日",
                    source=SourceRef(
                        file_id="file-1",
                        file_name="1.md",
                        source_path="目录/1.md",
                        section="规范",
                    ),
                    parse_version=old.markdown_path,
                    page_numbers=[1],
                )
            ]
        ),
    )
    session = RuleSession(
        RuleSet(project_id="project-1"),
        current,
        {"file-1": old},
        AsyncMock(),
        retrieval,
        rule_limit=env.rule_limit,
    )
    search, _, submit = build_rule_tools(session)
    assert set(search.tool_call_schema.model_json_schema()["properties"]) == {
        "querylist", "chunklist"
    }
    assert set(submit.tool_call_schema.model_json_schema()["properties"]) == {
        "operations",
        "project_profile",
        "return_view",
    }
    response = await search.ainvoke(
        tool_call(
            "search_documents",
            "search",
            {
                "querylist": [
                    {"queries": ["期限", "起算"], "file_ids": ["file-1"]},
                    {"queries": ["当前"], "file_ids": ["file-2"]},
                    {"queries": ["未来"], "file_ids": ["file-3"]},
                ]
            },
        )
    )
    assert response.content.count("期限原文90日") == 1
    assert response.content.count("### 片段 chunk-1") == 1
    assert "命中查询：期限；起算" in response.content
    assert "文件：file-1（1.md）" in response.content
    assert retrieval.search.await_count == 3
    assert "未执行" in response.content and "页码：1" in response.content
    assert session.accessed_history == {"file-1"}
    retrieval.search.return_value[0].parse_version = "different-version"
    response = await search.ainvoke(
        tool_call(
            "search_documents",
            "stale",
            {"querylist": [{"queries": ["期限"], "file_ids": ["file-1"]}]},
        )
    )
    assert "重建索引" in response.content
    assert "### 片段 chunk-1" not in response.content


@pytest.mark.asyncio
@pytest.mark.parametrize("first_fits", [True, False])
async def test_group_search_returns_full_hits_without_character_budget(env, first_fits):
    current = parsed_file(1)
    settings = env
    first = SearchResult(
        chunk_id="chunk-first",
        score=1,
        project_id="project-1",
        content="已展示的原文" if first_fits else "超长表格" * 2000,
        source=SourceRef(file_id="file-1", file_name="1.md", source_path="目录/1.md"),
        parse_version=current.markdown_path,
    )
    second = first.model_copy(
        update={"chunk_id": "chunk-second", "content": "后续长表格" * 2000}
    )
    retrieval = SimpleNamespace(
        settings=settings, search=AsyncMock(return_value=[first, second])
    )
    session = RuleSession(
        RuleSet(project_id="project-1"),
        current,
        {},
        AsyncMock(),
        retrieval,
        rule_limit=settings.rule_limit,
    )
    response = await build_rule_tools(session)[0].ainvoke(
        tool_call(
            "search_documents",
            "limited-search",
            {"querylist": [{"queries": ["参数"], "file_ids": ["file-1"]}]},
        )
    )
    assert "### 片段 chunk-second" in response.content
    assert second.content in response.content
    assert "### 片段 chunk-first" in response.content
    assert first.content in response.content
    assert "长度预算" not in response.content and "原文未展示" not in response.content
    assert session.accessed_history == {"file-1"}


@pytest.mark.asyncio
async def test_current_file_search_is_scoped_versioned_and_does_not_advance(env):
    current = parsed_file(1)
    retrieval = SimpleNamespace(
        settings=env,
        search=AsyncMock(
            return_value=[
                SearchResult(
                    chunk_id="chunk-1",
                    score=0.9,
                    project_id="project-1",
                    source=SourceRef(
                        file_id="file-1", file_name="1.md", source_path="目录/1.md"
                    ),
                    content="后续章节原文",
                    parse_version=current.markdown_path,
                ),
            ]
        ),
    )
    session = RuleSession(
        RuleSet(project_id="project-1"),
        current,
        {},
        AsyncMock(),
        retrieval,
        rule_limit=env.rule_limit,
    )
    search = build_rule_tools(session)[0]
    response = await search.ainvoke(
        tool_call(
            "search_documents",
            "search-current",
            {
                "querylist": [{"queries": ["参数"], "file_ids": ["file-1"]}],
            },
        )
    )
    assert "后续章节原文" in response.content and "不推进阅读进度" in response.content
    assert session.next_unit_index == 0
    filters = retrieval.search.call_args.args[1]
    assert filters.project_id == "project-1" and filters.file_ids == ["file-1"]
    assert str(filters.data_role) == "rule_source"
    retrieval.search.return_value[0].parse_version = "stale"
    response = await search.ainvoke(
        tool_call(
            "search_documents",
            "stale-current",
            {
                "querylist": [{"queries": ["参数"], "file_ids": ["file-1"]}],
            },
        )
    )
    assert "重建索引" in response.content


@pytest.mark.asyncio
@pytest.mark.parametrize("finish_reason", [None, "stop"])
@pytest.mark.parametrize(
    "content",
    [
        "本批规则和项目简介已保存。",
        "All rules were saved successfully.\n\n处理完毕",
        "  已完成本批阅读，无新增要求。\n",
    ],
)
async def test_completion_uses_saved_state_not_exact_phrase(
    env, content, finish_reason
):
    model = ScriptModel(
        [
            AIMessage(
                content="",
                tool_calls=[
                    tool_call(
                        "submit_rule_delta",
                        "save",
                        submit_args([add_operation()], view="changed"),
                    )
                ],
            ),
            AIMessage(
                content=content, response_metadata={"finish_reason": finish_reason}
            ),
            AIMessage(content="后续文件没有新要求，沿用已保存规则。"),
        ]
    )
    result = await extract(env, model, [parsed_file(1), parsed_file(2)])
    assert len(result.rules) == 1
    assert len(model.calls) == 3  # 不为收尾增加模型调用，也不要求无变化文件提交。
    path = env.storage.data_root / "parsed/rules/project-1.progress.json"
    progress = RuleExtractionProgress.model_validate_json(path.read_text())
    assert progress.completed_file_ids == ["file-1", "file-2"]
    assert progress.error is None and len(progress.commits) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [
        AIMessage(content="处理完毕", response_metadata={"finish_reason": "length"}),
        AIMessage(
            content="已保存", response_metadata={"finish_reason": "content_filter"}
        ),
        AIMessage(content="已保存", response_metadata={"finish_reason": "tool_calls"}),
        AIMessage(content=""),
        AIMessage(content=" \n\t"),
    ],
)
async def test_abnormal_or_empty_reply_cannot_finish(env, response):
    with pytest.raises(RuntimeError):
        await extract(env, ScriptModel([response]), [parsed_file(1)])
    path = env.storage.data_root / "parsed/rules/project-1.progress.json"
    progress = RuleExtractionProgress.model_validate_json(path.read_text())
    assert progress.error and not progress.completed_file_ids
    assert progress.next_unit_index == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("finish_reason", ["length", "tool_calls"])
async def test_diagnostics_log_invalid_arguments_without_partial_write(
    env, caplog, monkeypatch, finish_reason
):
    caplog.set_level("INFO", logger="app.llm.agent_tracing")
    raw = '{"operations":[{"requirement":"' + "不应泄漏的材料内容" * 3000
    response = AIMessage(
        content="",
        invalid_tool_calls=[
            {
                "name": "submit_rule_delta",
                "id": "bad",
                "args": raw,
                "error": "invalid JSON",
            }
        ],
        response_metadata={"finish_reason": finish_reason},
        usage_metadata={
            "input_tokens": 60000,
            "output_tokens": 8192,
            "total_tokens": 68192,
        },
    )
    model = ScriptModel(
        [
            AIMessage(
                content="",
                tool_calls=[
                    tool_call(
                        "submit_rule_delta",
                        "saved",
                        submit_args([add_operation()], view="changed"),
                    )
                ],
            ),
            response,
            AIMessage(content="未能完成"),
        ]
    )
    trace = LocalTrace("project-1")
    monkeypatch.setattr(trace, "write", lambda node, detail: None)
    token = current_trace.set(trace)
    try:
        with pytest.raises(RuntimeError, match="被截断|提交尚未成功"):
            await extract(env, model, [parsed_file(1)])
    finally:
        current_trace.reset(token)
    assert len(model.calls) == (2 if finish_reason == "length" else 3)
    logs = [r for r in caplog.records if r.name == "app.llm.agent_tracing"]
    requests = [
        json.loads(r.getMessage().split(" ", 1)[1])
        for r in logs
        if r.getMessage().startswith("agent_model_request ")
    ]
    responses = [r for r in logs if r.getMessage().startswith("agent_model_response ")]
    request = requests[1]
    result = json.loads(responses[1].getMessage().split(" ", 1)[1])
    assert request["generation_limits"] == {
        k: v
        for k, v in model.options[1].items()
        if k in {"max_tokens", "max_completion_tokens"}
    }
    assert request["estimated_input_tokens"] > 0
    assert result["project_id"] == "project-1" and result["file_id"] == "file-1"
    assert result["batch_index"] == 1 and result["turn_index"] == 2
    assert result["finish_reason"] == finish_reason and result["output_tokens"] == 8192
    assert result["invalid_tool_calls"][0]["arguments_chars"] == len(raw)
    assert responses[1].levelname == "WARNING"
    assert all("不应泄漏" not in r.getMessage() for r in logs)
    model_span = next(
        sid
        for sid, node in trace.nodes.items()
        if node["kind"] == "model" and node["turn_index"] == 2
    )
    assert trace.details[model_span]["output"]["diagnostics"] == result
    assert trace.details[model_span]["input"]["turn_index"] == request["turn_index"]
    path = env.storage.data_root / "parsed/rules/project-1.progress.json"
    progress = RuleExtractionProgress.model_validate_json(path.read_text())
    assert len(progress.rule_set.rules) == 1 and len(progress.commits) == 1


@pytest.mark.asyncio
async def test_diagnostics_failures_do_not_break_successful_response(
    env, monkeypatch, caplog
):
    caplog.set_level("INFO", logger="app.llm.agent_tracing")

    def fail(response):
        raise ValueError("unexpected diagnostics input")

    monkeypatch.setattr("app.llm.agent_tracing.response_diagnostics", fail)
    model = ScriptModel([AIMessage(content="处理完毕")])
    await extract(env, model, [parsed_file(1)])
    assert len(model.calls) == 1
    assert '"diagnostics_error": "ValueError"' in caplog.text


@pytest.mark.asyncio
async def test_failed_model_call_is_logged_and_not_retried(env, caplog):
    caplog.set_level("INFO", logger="app.llm.agent_tracing")

    class FailedModel(ScriptModel):
        async def invoke(self, messages, tools):
            self.calls.append((messages, tools))
            raise TimeoutError("sk-secret123456")

    model = FailedModel([])
    with pytest.raises(TimeoutError):
        await extract(env, model, [parsed_file(1)])
    assert len(model.calls) == 1
    entries = [
        r.getMessage() for r in caplog.records if r.name == "app.llm.agent_tracing"
    ]
    assert any(
        "agent_model_call_failed" in msg and "TimeoutError" in msg for msg in entries
    )
    assert not any("sk-secret" in msg for msg in entries)


@pytest.mark.asyncio
async def test_cross_project_input_rejected_before_model_call(env):
    foreign = parsed_file(1)
    foreign.document.project_id = "project-2"
    model = ScriptModel([])
    with pytest.raises(ValueError, match="不属于"):
        await extract(env, model, [foreign])
    assert not model.calls


@pytest.mark.asyncio
async def test_read_only_and_profile_only_do_not_rewrite_provenance(env):
    parsed = parsed_file(1)
    writer = AsyncMock()
    rules = RuleSet(
        project_id="project-1",
        source_files=[RuleSourceFile.from_document(parsed.document)],
    )
    session = RuleSession(
        rules,
        parsed,
        {},
        writer,
        SimpleNamespace(settings=env),
        rule_limit=env.rule_limit,
    )
    await session.submit(
        SubmitRuleDeltaInput(**submit_args([add_operation()], view="all")), "add"
    )
    session.rule_set.rules[0].provenance = "human_modified"
    await session.submit(SubmitRuleDeltaInput(**submit_args()), "read")
    assert writer.await_count == 1
    view = await session.submit(
        SubmitRuleDeltaInput(**submit_args(profile="新简介", view="changed")), "profile"
    )
    assert "新简介" in view and "rule-1" not in view
    assert session.rule_set.rules[0].provenance == "human_modified"


@pytest.mark.asyncio
async def test_replace_requires_read_access_to_new_historical_source(env):
    old, current = parsed_file(1), parsed_file(2)
    rules = RuleSet(
        project_id="project-1",
        source_files=[RuleSourceFile.from_document(p.document) for p in [old, current]],
    )
    session = RuleSession(
        rules,
        current,
        {"file-1": old},
        AsyncMock(),
        SimpleNamespace(settings=env),
        rule_limit=env.rule_limit,
    )
    await session.submit(
        SubmitRuleDeltaInput(**submit_args([add_operation()], view="changed")), "add"
    )
    operation = dict(
        action="replace",
        rule_id="rule-1",
        category="commercial",
        requirement="重新说明期限。",
        priority="important",
        verification="bid_only",
        reason="基于file-1及file-2",
        source_file_ids=["file-1", "file-2"],
    )
    request = SubmitRuleDeltaInput(**submit_args([operation], view="changed"))
    with pytest.raises(ValueError, match="先实际回查"):
        await session.submit(request, "replace")
    session.accessed_history.add("file-1")
    await session.submit(request, "replace")
    assert len(session.rule_set.rules[0].sources) == 2


def test_trace_redacts_copy_without_changing_actual_multimodal_message():
    from app.rules.tracing import trace_payload

    message = ToolMessage(
        tool_call_id="image-1",
        content=[
            {"type": "text", "text": "file-1/images/1.png"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,SECRET"}},
        ],
    )
    trace = trace_payload({"messages": [message]})
    assert "SECRET" not in str(trace)
    assert "file-1/images/1.png" in str(trace)
    assert message.content[1]["image_url"]["url"].endswith("SECRET")


@pytest.mark.asyncio
async def test_image_message_enters_agent_history_then_rule_can_be_saved(env):
    parsed = parsed_file(1)
    buffer = BytesIO()
    Image.new("RGB", (40, 40), "white").save(buffer, format="PNG")
    store = AssetStore(file_asset_root(env.storage.data_root, "file-1"))
    image = store.save_image(buffer.getvalue())
    parsed.sections[0].blocks = [
        ParsedBlock(kind="image", source_block_index=0, images=[image])
    ]
    model = ScriptModel(
        [
            AIMessage(
                content="",
                tool_calls=[tool_call("view_image", "picture", {"path": image.path})],
            ),
            AIMessage(
                content="",
                tool_calls=[
                    tool_call(
                        "submit_rule_delta",
                        "save",
                        submit_args([add_operation()], view="all"),
                    )
                ],
            ),
            AIMessage(content="处理完毕"),
        ]
    )
    client = MagicMock()
    trace_root = RunTree(name="local-test", run_type="chain", client=client)
    with tracing_context(enabled="local", parent=trace_root, client=client):
        result = await extract(env, model, [parsed])
    assert len(result.rules) == 1
    image_result = next(m for m in model.calls[1][0] if isinstance(m, ToolMessage))
    assert image_result.content[1]["type"] == "image_url"
    assert image_result.tool_call_id == "picture"
    progress = (
        env.storage.data_root / "parsed/rules/project-1.progress.json"
    ).read_text()
    assert "data:image" not in progress

    def flatten(run):
        return [run, *[node for child in run.child_runs for node in flatten(child)]]

    spans = flatten(trace_root)
    assert any(span.name == "rule_model" for span in spans)
    assert any(span.name == "view_image" for span in spans)
    assert "data:image" not in str([(span.inputs, span.outputs) for span in spans])
    client.create_run.assert_not_called()


@pytest.mark.asyncio
async def test_tool_calls_from_unbound_final_model_are_protocol_failure(env):
    calls = [
        AIMessage(
            content="",
            tool_calls=[tool_call("submit_rule_delta", f"read-{i}", submit_args())],
        )
        for i in range(3)
    ]
    model = ScriptModel(calls)
    with pytest.raises(RuntimeError, match="最终轮仍发出"):
        await extract(env, model, [parsed_file(1)])
    assert len(model.calls) == 3 and not model.calls[-1][1]


def mock_counter(monkeypatch):
    counter = TokenCounter("mock-characters", len, 100)
    monkeypatch.setattr(
        "app.rules.extractor.get_token_counter", lambda *a, **k: counter
    )
    return counter


def budget_env(env, capacity, *, steps=3):
    return env.model_copy(
        update={
            "rule_agent_max_steps": steps,
            "rule_extraction_model": env.rule_extraction_model.model_copy(
                update={
                    "context_window_tokens": capacity,
                    "compaction_trigger_ratio": 0.9,
                    "max_output_tokens": None,
                }
            ),
        }
    )


def read_progress(env):
    return RuleExtractionProgress.model_validate_json(
        (env.storage.data_root / "parsed/rules/project-1.progress.json").read_text()
    )


@pytest.mark.asyncio
async def test_small_window_batches_keep_complete_blocks_latest_view_and_progress(
    env, monkeypatch
):
    from app.llm.input_budget import BudgetSettings, check_input
    from app.rules.context import build_document_units
    from app.rules.extractor import _batch_context

    counter = mock_counter(monkeypatch)
    settings = budget_env(env, 100_000, steps=2)
    parsed = parsed_file(1)
    parsed.sections[0].blocks = [
        ParsedBlock(kind="text", source_block_index=0, content="FIRST:" + "A" * 3000),
        ParsedBlock(
            kind="table",
            source_block_index=1,
            content="|单位|阈值|\n|---|---|\n|" + "B" * 3000 + "|90日|",
        ),
        ParsedBlock(kind="text", source_block_index=2, content="THIRD:" + "C" * 3000),
    ]
    before = parsed.model_dump()
    session = RuleSession(
        RuleSet(project_id="project-1"),
        parsed,
        {},
        AsyncMock(),
        SimpleNamespace(settings=settings),
        rule_limit=settings.rule_limit,
    )
    session.batch_index = 1
    tools = [
        *build_rule_tools(session),
        *CommonTool(parsed_documents={"file-1": parsed}, settings=settings).tools(),
    ]
    context = _batch_context(
        session=session,
        units=build_document_units(parsed),
        start=0,
        end=1,
        tools=tools,
        settings=settings,
    )
    # 按 Mock 的固定输入测定容量，不写死 2000；额外留一点规则增长空间。
    cost = check_input(
        context=context, counter=counter, budget_settings=BudgetSettings(100_000)
    ).estimate.total
    settings = budget_env(settings, ceil((cost + 600) / 0.9), steps=2)
    write = tool_call(
        "submit_rule_delta", "one-write", submit_args([add_operation()], view="changed")
    )
    model = ScriptModel(
        [
            AIMessage(content="", tool_calls=[write]),
            AIMessage(content="处理完毕"),
            AIMessage(content="处理完毕"),
            AIMessage(content="处理完毕"),
        ]
    )
    progress = []
    result = await extract(settings, model, [parsed], progress.append)
    assert len(result.rules) == 1 and parsed.model_dump() == before
    assert "FIRST:" in str(model.calls[0][0]) and "THIRD:" not in str(model.calls[0][0])
    assert "B" * 3000 in str(model.calls[2][0])
    assert "rule-1" in model.calls[2][0][1].content
    assert "FIRST:" not in str(model.calls[2][0]) and "THIRD:" not in str(
        model.calls[2][0]
    )
    assert "THIRD:" in str(model.calls[3][0])
    for index in [2, 3]:
        assert not any(
            message.type in {"ai", "tool"} for message in model.calls[index][0]
        )
    final = progress[-1]
    assert (final.next_unit_index, final.batch_index, final.file_model_calls) == (
        3,
        3,
        4,
    )
    assert final.completed_file_ids == ["file-1"] and final.compaction_calls == 0
    assert all(p.completed_file_ids == [] for p in progress if p.next_unit_index < 3)
    assert all(option["max_tokens"] > 0 for option in model.options)


def compaction_scenario(env, monkeypatch, *, summary=None, second_read_size=14000, steps=2):
    from app.llm.input_budget import BudgetSettings, check_input
    from app.rules.extractor import _batch_context, build_document_units

    counter = mock_counter(monkeypatch)
    original_builder = build_rule_tools

    reads = 0

    async def load_mock_material():
        nonlocal reads
        reads += 1
        return "仅用于压缩测试的已查材料：" + "X" * (14000 if reads == 1 else second_read_size)

    tool = StructuredTool.from_function(
        coroutine=load_mock_material,
        name="load_mock_material",
        description="Mock 只读工具，返回已查材料。",
    )
    monkeypatch.setattr(
        "app.rules.extractor.build_rule_tools",
        lambda session: [*original_builder(session), tool],
    )
    parsed = parsed_file(1)
    parsed.sections[0].blocks[0].content = (
        "原文锚点-BEGIN:" + "D" * 30000 + ":原文锚点-END"
    )
    settings = budget_env(env, 100_000, steps=steps)
    session = RuleSession(
        RuleSet(project_id="project-1", source_files=[RuleSourceFile.from_document(parsed.document)]),
        parsed, {}, AsyncMock(), SimpleNamespace(settings=settings), rule_limit=settings.rule_limit,
    )
    tools = [
        *original_builder(session), tool,
        *CommonTool(parsed_documents={"file-1": parsed}, settings=settings).tools(),
    ]
    context = _batch_context(
        session=session, units=build_document_units(parsed), start=0, end=1,
        tools=tools, settings=settings,
    )
    fixed_cost = check_input(
        context=context, counter=counter, budget_settings=BudgetSettings(100_000),
    ).estimate.total
    # 容量随实际提示词/Schema固定开销浮动，避免文案增长误触硬上限。
    # 14K/15K只读结果仍须触发软阈值；下面的真实压缩次数断言验证此条件。
    settings = budget_env(settings, fixed_cost + 12_000, steps=steps)
    write = tool_call(
        "submit_rule_delta",
        "saved-before-compaction",
        submit_args([add_operation()], view="changed"),
    )
    read = tool_call("load_mock_material", "read-before-compaction", {})
    model = ScriptModel(
        [
            AIMessage(content="", tool_calls=[write, read]),
            summary
            if summary is not None
            else AIMessage(
                content="已保存 rule-1，来源 file-1；只读核对已完成。请根据已保存状态收尾。"
            ),
            AIMessage(content="处理完毕"),
        ]
    )
    return settings, parsed, model


@pytest.mark.asyncio
async def test_compaction_reinvokes_same_batch_preserves_write_and_final_round(
    env, monkeypatch
):
    settings, parsed, model = compaction_scenario(env, monkeypatch)
    result = await extract(settings, model, [parsed])
    assert len(result.rules) == 1 and len(model.calls) == 3
    assert model.calls[0][1] and model.calls[1][1] == model.calls[2][1] == []
    assert all(message.type in {"system", "human"} for message in model.calls[1][0])
    final_messages = model.calls[2][0]
    assert all(message.type in {"system", "human"} for message in final_messages)
    assert "rule-1" in final_messages[1].content
    assert parsed.sections[0].blocks[0].content in final_messages[1].content
    assert "已保存 rule-1" in final_messages[1].content
    assert "第 2/2 轮" in final_messages[1].content
    assert (
        sum("本轮不提供工具" in str(message.content) for message in final_messages) == 1
    )
    progress = read_progress(settings)
    assert progress.model_calls == progress.file_model_calls == 2
    assert progress.compaction_calls == 1 and len(progress.compaction_records) == 1
    assert progress.next_unit_index == 1 and progress.completed_file_ids == ["file-1"]
    assert len(progress.commits) == 1
    assert progress.commits[0].tool_call_id == "saved-before-compaction"
    assert (
        progress.compaction_records[0]["after_tokens"]
        < progress.compaction_records[0]["before_tokens"]
    )


@pytest.mark.asyncio
async def test_two_compactions_require_intervening_business_calls(env, monkeypatch):
    # 第二次没有前次提交的回执，增加Mock材料保持末轮仍触发软阈值。
    settings, parsed, initial = compaction_scenario(env, monkeypatch, second_read_size=15000, steps=3)
    first = next(initial.replies)
    model = ScriptModel(
        [
            first,
            # 模拟摘要错误推断轮次，代码仍应保留第 2/3 轮工具并提供权威提醒。
            AIMessage(
                content="rule-1 已保存。错误安排：下一轮是最后一轮，不可调用工具。"
            ),
            AIMessage(
                content="",
                tool_calls=[tool_call("load_mock_material", "second-read", {})],
            ),
            AIMessage(content="两次只读回查已完成，rule-1 已保存。请收尾。"),
            AIMessage(content="处理完毕"),
        ]
    )
    await extract(settings, model, [parsed])
    assert len(model.calls) == 5
    assert model.calls[2][1]  # 第 2/3 轮续跑仍有工具；不能误当成最终轮。
    assert "第 2/3 轮" in model.calls[2][0][1].content
    assert "不采纳摘要中的轮次安排" in model.calls[2][0][1].content
    assert model.calls[1][1] == model.calls[3][1] == model.calls[4][1] == []
    progress = read_progress(settings)
    assert progress.model_calls == progress.file_model_calls == 3
    assert progress.compaction_calls == 2 and len(progress.commits) == 1
    assert [record["step"] for record in progress.compaction_records] == [2, 3]


@pytest.mark.asyncio
async def test_invalid_read_can_recover_without_poisoning_saved_rules(env):
    settings = env.model_copy(update={"rule_agent_max_steps": 4})
    model = ScriptModel(
        [
            AIMessage(
                content="",
                tool_calls=[
                    tool_call(
                        "submit_rule_delta",
                        "write",
                        submit_args([add_operation()], view="changed"),
                    )
                ],
            ),
            AIMessage(
                content="",
                tool_calls=[
                    tool_call("submit_rule_delta", "bad-read", submit_args(view="all"))
                ],
            ),
            AIMessage(
                content="",
                tool_calls=[
                    tool_call("submit_rule_delta", "correct-read", submit_args())
                ],
            ),
            AIMessage(content="处理完毕"),
        ]
    )
    result = await extract(settings, model, [parsed_file(1)])
    assert len(result.rules) == 1
    progress = read_progress(settings)
    assert progress.completed_file_ids == ["file-1"] and len(progress.commits) == 1
    error = next(
        m
        for m in model.calls[2][0]
        if isinstance(m, ToolMessage) and m.tool_call_id == "bad-read"
    )
    assert error.status == "error" and "查看规则失败，没有发起写入" in error.content
    assert "提交失败" not in error.content


@pytest.mark.asyncio
async def test_reads_do_not_clear_a_real_unresolved_write_failure(env):
    model = ScriptModel(
        [
            AIMessage(
                content="",
                tool_calls=[
                    tool_call(
                        "submit_rule_delta",
                        "bad-write",
                        submit_args([add_operation()], view=None),
                    )
                ],
            ),
            AIMessage(
                content="",
                tool_calls=[
                    tool_call("submit_rule_delta", "bad-read", submit_args(view="all")),
                    tool_call("submit_rule_delta", "read", submit_args()),
                ],
            ),
            AIMessage(content="处理完毕"),
        ]
    )
    with pytest.raises(RuntimeError, match="规则提交尚未成功"):
        await extract(env, model, [parsed_file(1)])
    progress = read_progress(env)
    assert not progress.commits and not progress.completed_file_ids


@pytest.mark.asyncio
async def test_corrected_write_clears_real_failure(env):
    model = ScriptModel(
        [
            AIMessage(
                content="",
                tool_calls=[
                    tool_call(
                        "submit_rule_delta",
                        "bad-write",
                        submit_args([add_operation()], view=None),
                    )
                ],
            ),
            AIMessage(
                content="",
                tool_calls=[
                    tool_call(
                        "submit_rule_delta",
                        "correct-write",
                        submit_args([add_operation()], view="all"),
                    )
                ],
            ),
            AIMessage(content="处理完毕"),
        ]
    )
    result = await extract(env, model, [parsed_file(1)])
    assert len(result.rules) == 1
    progress = read_progress(env)
    assert progress.completed_file_ids == ["file-1"] and len(progress.commits) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "summary",
    [
        AIMessage(content=""),
        AIMessage(content="摘要半截", response_metadata={"finish_reason": "length"}),
        AIMessage(content="膨胀摘要" + "S" * 30000),
    ],
)
async def test_failed_compaction_keeps_commits_and_does_not_advance_or_loop(
    env, monkeypatch, summary
):
    settings, parsed, model = compaction_scenario(env, monkeypatch, summary=summary)
    with pytest.raises(InputBudgetError):
        await extract(settings, model, [parsed, parsed_file(2)])
    progress = read_progress(settings)
    assert len(model.calls) == 2 and progress.compaction_calls == 1
    assert progress.model_calls == progress.file_model_calls == 1
    assert progress.next_unit_index == 0 and progress.completed_file_ids == []
    assert progress.current_file_id == "file-1" and len(progress.commits) == 1
    assert len(progress.rule_set.rules) == 1 and progress.error


@pytest.mark.asyncio
async def test_progress_save_failure_does_not_publish_finished_cursor(env, monkeypatch):
    mock_counter(monkeypatch)
    progress = []

    async def persist(value):
        if value.next_unit_index > 0:
            raise OSError("Mock 进度保存失败")
        progress.append(value)

    model = ScriptModel([AIMessage(content="处理完毕")])
    with pytest.raises(OSError, match="进度保存失败"):
        await extract(env, model, [parsed_file(1), parsed_file(2)], persist)
    assert len(model.calls) == 1
    assert progress[-1].next_unit_index == 0
    assert progress[-1].completed_file_ids == [] and progress[-1].error


@pytest.mark.asyncio
async def test_oversize_atomic_block_stops_without_call_or_skip(env, monkeypatch):
    mock_counter(monkeypatch)
    settings = budget_env(env, 1000)
    parsed = parsed_file(1)
    parsed.sections[0].blocks[0].content = "HUGE" * 1000
    model = ScriptModel([])
    with pytest.raises(InputBudgetError, match="完整结构"):
        await extract(settings, model, [parsed, parsed_file(2)])
    assert not model.calls
    progress = read_progress(settings)
    assert progress.next_unit_index == 0 and progress.completed_file_ids == []


@pytest.mark.asyncio
async def test_empty_blocks_do_not_masquerade_as_completed_document(env):
    parsed = parsed_file(1)
    parsed.sections[0].blocks[0].content = "  "
    model = ScriptModel([])
    with pytest.raises(ValueError, match="没有可阅读"):
        await extract(env, model, [parsed])
    assert not model.calls
