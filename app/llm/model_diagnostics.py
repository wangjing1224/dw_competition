"""通用模型调用只读诊断；业务方仅提供身份和预算，不输出正文或推理内容。"""

import json


def budget_diagnostics(controller):
    """读取已有预算结果，不触发额外分词或改变控制器状态。"""
    decision = controller.previous
    estimate = decision.estimate if decision else None
    settings = controller.settings
    return {
        "model_name": settings.model,
        "platform": settings.platform,
        "thinking_enabled": settings.enable_thinking,
        "estimated_input_tokens": estimate.total if estimate else None,
        "estimate_method": estimate.method if estimate else None,
        "context_window_tokens": settings.context_window_tokens,
        "input_threshold_tokens": controller.budget.threshold,
    }


def request_diagnostics(messages, tools, call_options, metadata):
    # metadata 可能包含正文、Key 或核验要求，只取身份、预算的白名单标量。
    allowed = {
        "project_id",
        "bidder_id",
        "task_id",
        "file_id",
        "rule_id",
        "session_id",
        "child_session",
        "batch_index",
        "turn_index",
        "check_index",
        "attempt",
        "model_name",
        "platform",
        "thinking_enabled",
        "estimated_input_tokens",
        "estimate_method",
        "context_window_tokens",
        "input_threshold_tokens",
    }
    return {
        **{
            key: value
            for key, value in metadata.items()
            if key in allowed
            and (value is None or type(value) in {str, int, float, bool})
        },
        "message_count": len(messages),
        "generation_limits": {
            key: call_options[key]
            for key in ("max_tokens", "max_completion_tokens")
            if key in call_options
        },
        "tools_enabled": bool(tools),
        "tool_names": [tool.name for tool in tools],
    }


def _argument_metrics(call, raw_arguments=None):
    args = call.get("args")
    if isinstance(raw_arguments, str):
        text, source = raw_arguments, "raw"
    elif isinstance(args, str):
        text, source = args, "raw"
    elif args is not None:
        text = json.dumps(args, ensure_ascii=False, separators=(",", ":"))
        source = "reserialized"
    else:
        text, source = None, "missing"
    metrics = {
        "tool_call_id": call.get("id"),
        "name": call.get("name"),
        # 重新序列化的长度不能冒充服务端原始 JSON 长度，更不能当成 Token 数。
        "arguments_source": source,
        "arguments_chars": len(text) if text is not None else None,
        "arguments_utf8_bytes": len(text.encode("utf-8", errors="replace"))
        if text is not None
        else None,
        "list_argument_counts": None,
        "json_valid": None,
    }
    if text is None:
        return metrics
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        metrics.update(
            json_valid=False,
            # 不记录 call.error，它可能带出整段工具参数或业务正文。
            json_error={
                "message": exc.msg,
                "position": exc.pos,
                "line": exc.lineno,
                "column": exc.colno,
            },
        )
    except (ValueError, RecursionError) as exc:
        metrics["json_check_error"] = type(exc).__name__
    else:
        metrics["json_valid"] = True
        # operations、querylist、checks、edits 等统一计数，不耦合某一种工具协议。
        metrics["list_argument_counts"] = (
            {
                key: len(value)
                for key, value in parsed.items()
                if isinstance(value, list)
            }
            if isinstance(parsed, dict)
            else None
        )
    return metrics


def response_diagnostics(response):
    metadata = response.response_metadata or {}
    usage = response.usage_metadata or {}
    provider_usage = metadata.get("token_usage") or {}

    def token_count(normalized, provider):
        value = usage.get(normalized)
        if value is None:
            value = provider_usage.get(provider)
        # 未提供必须保留 null，不能误报为 0，也不原样复制不受控 metadata。
        return value if type(value) is int else None

    reasoning_tokens = (usage.get("output_token_details") or {}).get("reasoning")
    if reasoning_tokens is None:
        reasoning_tokens = (provider_usage.get("completion_tokens_details") or {}).get(
            "reasoning_tokens"
        )
    raw_args = {
        call.get("id"): (call.get("function") or {}).get("arguments")
        for call in response.additional_kwargs.get("tool_calls", [])
        if isinstance(call, dict) and call.get("id")
    }
    valid = [
        _argument_metrics(call, raw_args.get(call.get("id")))
        for call in response.tool_calls
    ]
    invalid = [_argument_metrics(call) for call in response.invalid_tool_calls]
    lengths = [
        call["arguments_chars"]
        for call in [*valid, *invalid]
        if call["arguments_chars"] is not None
    ]
    content = response.content
    text_chars = (
        len(content)
        if isinstance(content, str)
        else sum(
            len(block["text"])
            for block in content
            if isinstance(block, dict)
            and block.get("type") == "text"
            and isinstance(block.get("text"), str)
        )
    )
    reasoning = response.additional_kwargs.get("reasoning_content")
    return {
        "finish_reason": metadata.get("finish_reason"),
        "input_tokens": token_count("input_tokens", "prompt_tokens"),
        "output_tokens": token_count("output_tokens", "completion_tokens"),
        "total_tokens": token_count("total_tokens", "total_tokens"),
        "reasoning_tokens": reasoning_tokens if type(reasoning_tokens) is int else None,
        "content_text_chars": text_chars,
        "reasoning_chars": len(reasoning) if isinstance(reasoning, str) else None,
        "tool_calls_count": len(valid),
        "invalid_tool_calls_count": len(invalid),
        "arguments_chars_total": sum(lengths),
        "arguments_chars_max": max(lengths, default=0),
        # 防止多工具响应把诊断日志撑大；总计仍覆盖全部调用。
        "tool_calls": valid[:20],
        "invalid_tool_calls": invalid[:20],
        "call_details_truncated": len(valid) > 20 or len(invalid) > 20,
    }
