"""诊断保留可定位的数字，不复制长参数、密钥、图片或推理正文。"""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from langchain_core.messages import AIMessage

from app.llm.agent_tracing import invoke_agent_model
from app.llm.model_diagnostics import response_diagnostics


def test_raw_argument_lengths_and_normalized_usage_do_not_mutate_response():
    raw = '{ "operations": [{"requirement": "不公开的规则"}], "project_profile": null }'
    response = AIMessage(
        content="准备保存",
        tool_calls=[
            {"name": "submit_rule_delta", "id": "call-1", "args": json.loads(raw)}
        ],
        additional_kwargs={
            "tool_calls": [{"id": "call-1", "function": {"arguments": raw}}],
            "reasoning_content": "不公开的推理",
        },
        usage_metadata={
            "input_tokens": 100,
            "output_tokens": 20,
            "total_tokens": 120,
            "output_token_details": {"reasoning": 3},
        },
        response_metadata={
            "finish_reason": "tool_calls",
            "token_usage": {"completion_tokens": 999},
        },
    )
    original = response.model_dump()
    result = response_diagnostics(response)
    assert result["input_tokens"] == 100 and result["output_tokens"] == 20
    assert result["reasoning_tokens"] == 3
    assert result["tool_calls_count"] == 1
    detail = result["tool_calls"][0]
    assert detail["arguments_source"] == "raw"
    assert detail["arguments_chars"] == len(raw)
    assert detail["arguments_utf8_bytes"] == len(raw.encode())
    assert detail["json_valid"] is True
    assert detail["list_argument_counts"] == {"operations": 1}
    assert "不公开" not in json.dumps(result, ensure_ascii=False)
    assert response.model_dump() == original


def test_truncated_json_only_reports_lengths_and_error_position():
    raw = (
        '{"operations":[{"requirement":"'
        + "很长的要求" * 10000
        + "sk-secret123456 data:image/png;base64,abc"
    )
    response = AIMessage(
        content="",
        invalid_tool_calls=[
            {"name": "submit_rule_delta", "id": "bad-1", "args": raw, "error": raw}
        ],
        response_metadata={
            "finish_reason": "length",
            "token_usage": {
                "prompt_tokens": 60000,
                "completion_tokens": 8192,
                "total_tokens": 68192,
                "completion_tokens_details": {"reasoning_tokens": 0},
            },
        },
    )
    result = response_diagnostics(response)
    assert result["finish_reason"] == "length"
    assert result["input_tokens"] == 60000 and result["output_tokens"] == 8192
    assert result["reasoning_tokens"] == 0
    assert result["arguments_chars_total"] == result["arguments_chars_max"] == len(raw)
    assert result["tool_calls_count"] == 0 and result["invalid_tool_calls_count"] == 1
    detail = result["invalid_tool_calls"][0]
    assert detail["json_valid"] is False
    assert detail["json_error"]["position"] > 0
    assert detail["list_argument_counts"] is None  # 不猜半截 JSON 含有多少完整操作。
    encoded = json.dumps(result, ensure_ascii=False)
    assert len(encoded) < 2000
    assert (
        "很长的要求" not in encoded
        and "sk-secret" not in encoded
        and "base64" not in encoded
    )


def test_missing_usage_is_not_zero_and_reserialized_lengths_are_labelled():
    response = AIMessage(
        content="",
        tool_calls=[
            {"name": "submit_rule_delta", "id": "1", "args": {"operations": []}}
        ],
    )
    result = response_diagnostics(response)
    assert result["input_tokens"] is None and result["output_tokens"] is None
    assert result["tool_calls"][0]["arguments_source"] == "reserialized"
    assert result["tool_calls"][0]["arguments_chars"] == len('{"operations":[]}')
    assert result["finish_reason"] is None


def test_zero_usage_is_preserved_and_multimodal_content_is_not_logged():
    response = AIMessage(
        content=[
            {"type": "text", "text": "保存结束"},
            {
                "type": "image_url",
                "image_url": {"url": "data:image/png;base64,secret-image"},
            },
        ],
        usage_metadata={"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
        response_metadata={
            "token_usage": {"prompt_tokens": 99, "completion_tokens": 99}
        },
    )
    result = response_diagnostics(response)
    assert result["content_text_chars"] == 4
    assert result["input_tokens"] == result["output_tokens"] == 0
    assert "secret-image" not in json.dumps(result)


def test_call_detail_limit_does_not_truncate_aggregate_counts():
    response = AIMessage(
        content="",
        tool_calls=[
            {
                "name": "submit_rule_delta",
                "id": str(i),
                "args": {"operations": [{"action": "add"}]},
            }
            for i in range(25)
        ],
    )
    result = response_diagnostics(response)
    assert result["tool_calls_count"] == 25
    assert len(result["tool_calls"]) == 20 and result["call_details_truncated"]
    assert result["arguments_chars_total"] == result["arguments_chars_max"] * 25


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "role,field",
    [
        ("rule_model", "operations"),
        ("evidence_ingestion", "file_ids"),
        ("bid_agent", "edits"),
        ("evidence_subagent", "querylist"),
        ("bid_route", "checks"),
    ],
)
async def test_shared_diagnostics_cover_roles_and_are_not_sent_to_model(
    caplog, role, field
):
    caplog.set_level("INFO", logger="app.llm.agent_tracing")
    response = AIMessage(
        content="业务正文不应进入普通日志",
        tool_calls=[
            {
                "name": "test_tool",
                "id": "tool-1",
                "args": {field: ["第一项", "第二项"]},
            }
        ],
        response_metadata={"finish_reason": "tool_calls"},
    )
    model = SimpleNamespace(ainvoke=AsyncMock(return_value=response))
    diagnostics = {}
    messages = [{"role": "user", "content": "原始文档正文"}]
    options = {"max_tokens": 4096, "extra_body": {"thinking": {"type": "disabled"}}}
    returned = await invoke_agent_model(
        model,
        messages,
        [],
        {
            "enabled": False,
            "metadata": {
                "task_id": "audit-1",
                "bidder_id": "bidder-1",
                "file_id": "file-1",
                "api_key": "sk-secret123456",
                "verification_request": "业务核验要求",
            },
        },
        trace_name=role,
        diagnostic_context={
            "turn_index": 2,
            "estimated_input_tokens": 1000,
            "messages": "不应复制上下文",
        },
        diagnostics=diagnostics,
        **options,
    )
    assert returned is response
    model.ainvoke.assert_awaited_once_with(messages, **options)
    entries = [
        r.getMessage() for r in caplog.records if r.name == "app.llm.agent_tracing"
    ]
    assert len(entries) == 2
    request, result = [json.loads(entry.split(" ", 1)[1]) for entry in entries]
    assert request["call_id"] == result["call_id"]
    assert result["role"] == role and result["turn_index"] == 2
    assert result["task_id"] == "audit-1" and result["bidder_id"] == "bidder-1"
    assert result["generation_limits"] == {"max_tokens": 4096}
    assert result["tool_calls"][0]["list_argument_counts"] == {field: 2}
    assert diagnostics == result
    for text in [
        "业务正文",
        "原始文档",
        "不应复制上下文",
        "业务核验要求",
        "sk-secret",
        "第一项",
    ]:
        assert not any(text in entry for entry in entries)
