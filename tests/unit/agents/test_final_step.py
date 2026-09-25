"""最后一轮调用前撤掉工具；错误不变成人工审核业务结论。"""

import asyncio

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_core.tools import StructuredTool

from app.audit.session import prepare_request, run_session
from tests.helpers.audit import SequenceModel, tool_response


def messages(handoff=""):
    return [
        SystemMessage(content="核验事实"),
        HumanMessage(content="当前材料。" + handoff),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["bid_agent", "evidence_subagent"])
async def test_penultimate_tools_and_final_plain_content(temp_settings, role):
    async def lookup(query: str):
        return "原文"

    tool = StructuredTool.from_function(coroutine=lookup, description="查询原文")
    model = SequenceModel(
        [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "lookup",
                        "args": {"query": "A"},
                        "id": "a",
                        "type": "tool_call",
                    },
                    {
                        "name": "lookup",
                        "args": {"query": "B"},
                        "id": "b",
                        "type": "tool_call",
                    },
                ],
            ),
            "处理完毕",
        ]
    )
    result = await run_session(
        model=model,
        tools=[tool],
        settings=temp_settings.bid_chat_model,
        max_steps=2,
        role=role,
        limiter=asyncio.Semaphore(1),
        trace_context={},
        build_messages=messages,
    )
    assert result == "处理完毕"
    assert model.bindings == [["lookup"]]
    assert [m.tool_call_id for m in model.calls[1][0] if m.type == "tool"] == ["a", "b"]
    assert model.calls[1][0][-1].type == "human"
    assert "2" in model.calls[1][0][-1].content
    assert model.calls[0][1]["max_tokens"] > 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response,match",
    [
        (AIMessage(content=""), "非空"),
        (
            AIMessage(content="截断", response_metadata={"finish_reason": "length"}),
            "截断",
        ),
        (tool_response("unknown", {}), "无工具末轮"),
        (RuntimeError("HTTP 402"), "HTTP 402"),
    ],
)
async def test_failure_is_not_fabricated_manual_review(temp_settings, response, match):
    model = SequenceModel([response])
    with pytest.raises(RuntimeError, match=match):
        await run_session(
            model=model,
            tools=[],
            settings=temp_settings.bid_chat_model,
            max_steps=1,
            role="bid_agent",
            limiter=asyncio.Semaphore(1),
            trace_context={},
            build_messages=messages,
        )
    assert len(model.calls) == 1
    assert not model.bindings


@pytest.mark.asyncio
async def test_validation_error_closes_tool_message_before_next_call(temp_settings):
    async def lookup(query: str):
        raise ValueError("查询范围不合法")

    tool = StructuredTool.from_function(coroutine=lookup, description="查原文")
    model = SequenceModel([tool_response("lookup", {"query": "错误"}), "无法确认"])
    assert (
        await run_session(
            model=model,
            tools=[tool],
            settings=temp_settings.bid_chat_model,
            max_steps=2,
            role="evidence_subagent",
            limiter=asyncio.Semaphore(1),
            trace_context={},
            build_messages=messages,
        )
        == "无法确认"
    )
    tool_message = model.calls[1][0][-2]
    assert tool_message.type == "tool" and tool_message.tool_call_id == "call-1"
    assert tool_message.status == "error"


def test_limit_is_checked_before_model_call(temp_settings):
    with pytest.raises(RuntimeError, match="轮次上限"):
        prepare_request(
            {"messages": messages(), "model_calls": 8},
            tools=[],
            max_steps=8,
            settings=temp_settings.bid_chat_model,
            role="bid_agent",
        )
