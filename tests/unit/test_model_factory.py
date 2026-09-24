"""模型工厂参数契约测试。"""

import json
from functools import partial

import httpx
import pytest
from langchain_core.messages import (
    AIMessageChunk,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from pydantic import SecretStr

from app.llm import factory
from app.llm.input_budget import InputContext
from app.llm.platforms import ChatProtocol, PlatformChatOpenAI


@pytest.mark.parametrize(
    "platform,model_name,token_key",
    [
        ("deepseek", "deepseek-v4-flash", "max_tokens"),
        ("bailian", "qwen3.6-plus", "max_completion_tokens"),
        ("openai", "gpt-5.1", "max_completion_tokens"),
    ],
)
@pytest.mark.parametrize("thinking", [False, True])
def test_budgeted_and_fixed_requests_use_platform_parameters(
    temp_settings, platform, model_name, token_key, thinking
):
    settings = temp_settings.rule_extraction_model.model_copy(
        update={
            "api_key": SecretStr("test-key"),
            "platform": platform,
            "model": model_name,
            "enable_thinking": thinking,
        }
    )
    real = factory.build_chat_model(settings)
    initial = real._get_request_payload([HumanMessage(content="测试")])
    assert "max_tokens" not in initial and "max_completion_tokens" not in initial
    options = factory.budgeted_generation_options(settings, 9000)
    payload = real._get_request_payload([HumanMessage(content="测试")], **options)
    assert payload[token_key] == 9000
    other_key = (
        "max_tokens"
        if token_key == "max_completion_tokens"
        else "max_completion_tokens"
    )
    assert other_key not in payload
    if platform == "deepseek":
        assert payload["extra_body"] == {
            "thinking": {"type": "enabled" if thinking else "disabled"}
        }
    elif platform == "bailian":
        assert payload["extra_body"] == {
            "enable_thinking": thinking,
            "preserve_thinking": thinking,
        }
        assert real.streaming == thinking
    else:
        assert payload.get("extra_body") is None
        assert payload["reasoning_effort"] == ("medium" if thinking else "none")
    if thinking and platform != "bailian":
        assert "temperature" not in payload
    fixed = factory.build_chat_model(
        settings.model_copy(
            update={"context_window_tokens": None, "max_output_tokens": 4096}
        )
    )
    assert fixed._get_request_payload([HumanMessage(content="测试")])[token_key] == 4096


def test_openai_non_reasoning_and_unsupported_toggle(temp_settings):
    cfg = temp_settings.bid_chat_model.model_copy(
        update={"api_key": SecretStr("test"), "platform": "openai", "model": "gpt-4.1"}
    )
    payload = factory.build_chat_model(cfg)._get_request_payload(
        [HumanMessage(content="测试")]
    )
    assert not payload.get("extra_body") and "reasoning_effort" not in payload
    with pytest.raises(ValueError, match="不支持关闭"):
        factory.build_chat_model(cfg.model_copy(update={"model": "o3"}))
    with pytest.raises(ValueError, match="尚未登记"):
        factory.build_chat_model(
            cfg.model_copy(update={"model": "private-alias", "enable_thinking": True})
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "platform,model_name",
    [
        ("deepseek", "deepseek-v4-flash"),
        ("bailian", "qwen3.6-plus"),
        ("openai", "gpt-5.1"),
    ],
)
@pytest.mark.parametrize("thinking", [False, True])
async def test_real_http_serialization_and_reasoning_replay(
    monkeypatch, temp_settings, platform, model_name, thinking
):
    """截获 SDK 最终 HTTP JSON，覆盖流式/非流式、工具闭合、回放及无工具收尾。"""
    requests = []
    answer = {
        "role": "assistant",
        "content": "已查明",
        "reasoning_content": "测试推理片段",
        "tool_calls": [
            {
                "id": "lookup-1",
                "type": "function",
                "function": {"name": "lookup", "arguments": "{}"},
            }
        ],
    }

    def respond(request):
        body = json.loads(request.content)
        requests.append(body)
        usage = {
            "prompt_tokens": 120,
            "completion_tokens": 40,
            "total_tokens": 160,
            "completion_tokens_details": {"reasoning_tokens": 10},
        }
        common = {"id": "chatcmpl-test", "model": model_name, "created": 0}
        if body.get("stream"):
            delta = {**answer, "tool_calls": [{"index": 0, **answer["tool_calls"][0]}]}
            chunks = [
                {
                    **common,
                    "object": "chat.completion.chunk",
                    "choices": [{"index": 0, "delta": delta, "finish_reason": None}],
                },
                {
                    **common,
                    "object": "chat.completion.chunk",
                    "choices": [
                        {"index": 0, "delta": {}, "finish_reason": "tool_calls"}
                    ],
                    "usage": usage,
                },
            ]
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                text="".join("data: " + json.dumps(c) + "\n\n" for c in chunks)
                + "data: [DONE]\n\n",
            )
        return httpx.Response(
            200,
            json={
                **common,
                "object": "chat.completion",
                "usage": usage,
                "choices": [
                    {"index": 0, "message": answer, "finish_reason": "tool_calls"}
                ],
            },
        )

    transport = httpx.MockTransport(respond)
    with httpx.Client(transport=transport) as sync:
        async with httpx.AsyncClient(transport=transport) as client:
            monkeypatch.setattr(
                factory,
                "PlatformChatOpenAI",
                partial(PlatformChatOpenAI, http_client=sync, http_async_client=client),
            )
            cfg = temp_settings.bid_chat_model.model_copy(
                update={
                    "base_url": "https://mock.test/v1",
                    "api_key": SecretStr("test-key"),
                    "platform": platform,
                    "model": model_name,
                    "enable_thinking": thinking,
                }
            )
            model = factory.build_chat_model(cfg)
            tool = {
                "type": "function",
                "function": {
                    "name": "lookup",
                    "description": "测试",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
            messages = [SystemMessage(content="测试"), HumanMessage(content="调用工具")]
            reply = await model.bind_tools([tool]).ainvoke(messages)
            assert reply.tool_calls[0]["id"] == "lookup-1"
            assert reply.usage_metadata["input_tokens"] == 120
            assert ("reasoning_content" in reply.additional_kwargs) == (
                platform != "openai"
            )
            messages.extend(
                [reply, ToolMessage(content="结果", tool_call_id="lookup-1")]
            )
            await model.bind_tools([tool]).ainvoke(messages)
            context = InputContext(
                tuple(messages), {"tools": [tool]}, ChatProtocol.from_settings(cfg)
            )
            assert requests[1]["messages"] == context.wire_messages()
            assert requests[1]["tools"] == context.bindings["tools"]
            replayed = requests[1]["messages"][-2]
            assert ("reasoning_content" in replayed) == (
                thinking and platform != "openai"
            )
            if platform == "deepseek":
                assert requests[1]["thinking"]["type"] == (
                    "enabled" if thinking else "disabled"
                )
                assert (
                    "enable_thinking" not in requests[1]
                    and "max_completion_tokens" not in requests[1]
                )
            else:
                assert (
                    requests[1]["max_completion_tokens"] == cfg.max_output_tokens
                    and "thinking" not in requests[1]
                )
            await model.ainvoke(messages)
            final_context = InputContext(
                tuple(messages), protocol=ChatProtocol.from_settings(cfg)
            )
            assert requests[2]["messages"] == final_context.wire_messages()
            assert not requests[2].get("tools")
            if platform in {"deepseek", "openai"}:
                assert "reasoning_content" not in requests[2]["messages"][-2]


def test_streamed_reasoning_chunks_are_not_lost():
    model = PlatformChatOpenAI(
        model="deepseek-v4-flash",
        api_key="test",
        protocol=ChatProtocol("deepseek", "deepseek-v4-flash", True),
        use_responses_api=False,
    )
    chunks = [
        model._convert_chunk_to_generation_chunk(
            {
                "choices": [
                    {
                        "delta": {
                            "role": "assistant",
                            "content": "",
                            "reasoning_content": text,
                        },
                        "finish_reason": None,
                    }
                ]
            },
            AIMessageChunk,
            None,
        ).message
        for text in ("片段一", "片段二")
    ]
    assert (chunks[0] + chunks[1]).additional_kwargs[
        "reasoning_content"
    ] == "片段一片段二"


def test_openai_developer_role_is_counted_the_same(temp_settings):
    cfg = temp_settings.bid_chat_model.model_copy(
        update={
            "platform": "openai",
            "model": "o3",
            "enable_thinking": True,
            "api_key": SecretStr("test"),
        }
    )
    messages = [SystemMessage(content="约束"), HumanMessage(content="问题")]
    payload = factory.build_chat_model(cfg)._get_request_payload(messages)
    assert (
        payload["messages"]
        == InputContext(
            tuple(messages), protocol=ChatProtocol.from_settings(cfg)
        ).wire_messages()
    )
    assert payload["messages"][0]["role"] == "developer"


def test_llama_cpp_embedding_does_not_send_dimensions(
    monkeypatch,
    temp_settings,
):
    """llama.cpp 不支持 dimensions，维度只在本地用于响应校验。"""

    captured = {}

    class FakeOpenAIEmbeddings:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(factory, "OpenAIEmbeddings", FakeOpenAIEmbeddings)

    model = factory.build_embedding_model(temp_settings.embedding_model)

    assert "dimensions" not in captured
    assert model.dimension == 1024
