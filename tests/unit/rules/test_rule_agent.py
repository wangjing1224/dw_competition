"""真实图/工具闭环配合脚本模型，不连接外部模型。"""

from io import BytesIO
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from langchain_core.messages import AIMessage, ToolMessage
from langsmith import tracing_context
from langsmith.run_trees import RunTree
from PIL import Image

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

    def bind_tools(self, tools):
        async def invoke(messages):
            return await self.invoke(messages, [tool.name for tool in tools])

        return SimpleNamespace(ainvoke=invoke)

    async def ainvoke(self, messages):
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
                    call["id"] for call in getattr(message, "tool_calls", [])
                )
        assert not pending
        self.calls.append((list(messages), tools))
        return next(self.replies)


@pytest.fixture
def env(monkeypatch, temp_settings):
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
    assert len(initial_second) == 2 and "rule-1" in initial_second[0].content
    assert "文件1：期限" not in initial_second[1].content
    tools = [m for m in model.calls[1][0] if isinstance(m, ToolMessage)]
    assert len(tools) == 2 and all("rule-1" in m.content for m in tools)
    assert progress[-1].completed_file_ids == ["file-1", "file-2"]
    assert any(p.rule_set.rules and not p.completed_file_ids for p in progress)
    assert progress[-1].rule_set.rules[0].provenance == "ai_generated"


@pytest.mark.asyncio
@pytest.mark.parametrize("steps", [3, 8])
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
    assert error.status == "error" and "未修改" in error.content


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
    session = RuleSession(rules, parsed, {}, writer, SimpleNamespace(settings=env))
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
    )
    search, submit = build_rule_tools(session)
    assert set(search.tool_call_schema.model_json_schema()["properties"]) == {
        "querylist"
    }
    assert set(submit.tool_call_schema.model_json_schema()["properties"]) == {
        "operations",
        "project_profile",
        "return_view",
    }
    response = await search.ainvoke(
        tool_call(
            "search_rule_source",
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
    assert retrieval.search.await_count == 2
    assert "未执行" in response.content and "页码：1" in response.content
    assert session.accessed_history == {"file-1"}
    retrieval.search.return_value[0].parse_version = "different-version"
    response = await search.ainvoke(
        tool_call(
            "search_rule_source",
            "stale",
            {"querylist": [{"queries": ["期限"], "file_ids": ["file-1"]}]},
        )
    )
    assert "重建索引" in response.content


@pytest.mark.asyncio
async def test_truncation_or_unprocessed_reply_cannot_finish(env):
    for response in [
        AIMessage(content="处理完毕", response_metadata={"finish_reason": "length"}),
        AIMessage(content="仍有未处理的附表"),
    ]:
        with pytest.raises(RuntimeError):
            await extract(env, ScriptModel([response]), [parsed_file(1)])


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
    session = RuleSession(rules, parsed, {}, writer, SimpleNamespace(settings=env))
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
        rules, current, {"file-1": old}, AsyncMock(), SimpleNamespace(settings=env)
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
