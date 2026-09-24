"""纯本地小窗口验证；不依赖业务文件、不发真实模型请求。"""

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_openai import ChatOpenAI

from app.llm.context_compaction import (
    InputBudgetError,
    InputController,
    compact_context,
)
from app.llm.input_budget import (
    BudgetSettings,
    InputContext,
    check_input,
    plan_document_input,
    usage_baseline_from_response,
)
from app.llm.model_capabilities import ModelCapabilities, output_allowance
from app.llm.platforms import ChatProtocol
from app.llm.token_counter import TokenCounter, get_token_counter


def ctx(*messages, bindings=None):
    return InputContext(tuple(messages), bindings or {})


def call(key):
    return {"name": "search", "id": key, "args": {"query": "参数"}, "type": "tool_call"}


def image_message(length=100):
    return HumanMessage(
        content=[
            {"type": "text", "text": "file-1/images/1.png"},
            {
                "type": "image_url",
                "image_url": {"url": "data:image/png;base64," + "A" * length},
            },
        ]
    )


@pytest.fixture
def counter():
    return TokenCounter("fake-characters", Mock(side_effect=len), 100)


def check(context, counter, **kwargs):
    return check_input(
        context=context,
        counter=counter,
        budget_settings=BudgetSettings(5000, model_key="test"),
        **kwargs,
    )


def test_four_views_are_read_only_and_interleaved(counter):
    messages = [
        SystemMessage(content="稳定要求及提示词内 JSON Schema"),
        HumanMessage(content="原始材料"),
        AIMessage(content="", tool_calls=[call("a"), call("b")]),
        ToolMessage(content="resultA", tool_call_id="a"),
        ToolMessage(content="resultB", tool_call_id="b"),
        HumanMessage(content="后续补充要求"),
        AIMessage(content="普通回复"),
    ]
    bindings = {"tools": [{"type": "function", "function": {"name": "search"}}]}
    context = ctx(*messages, bindings=bindings)
    before = [message.model_dump() for message in messages]
    result = check(context, counter)
    assert result.status == "ready"
    assert set(result.estimate.parts) == {"system", "bindings", "human", "interaction"}
    assert all(value > 0 for value in result.estimate.parts.values())
    assert [message.model_dump() for message in messages] == before
    assert list(context.messages) == messages and context.bindings == bindings
    counted = "\n".join(args.args[0] for args in counter.count_text.call_args_list)
    assert counted.count("稳定要求及提示词内 JSON Schema") == 1
    assert counted.count("resultA") == 1 and '"arguments"' in counted


def test_actual_request_serialization_matches_langchain(counter):
    model = ChatOpenAI(model="test-model", api_key="test-key", use_responses_api=False)
    context = ctx(
        SystemMessage(content="政策"),
        HumanMessage(content="任务"),
        AIMessage(content="", tool_calls=[call("a")]),
        ToolMessage(content="结果", name="search", tool_call_id="a"),
        bindings={
            "tools": [
                {
                    "type": "function",
                    "function": {"name": "search", "parameters": {"type": "object"}},
                }
            ]
        },
    )
    payload = model._get_request_payload(list(context.messages), **context.bindings)
    assert payload["messages"] == context.wire_messages()
    assert payload["tools"] == context.bindings["tools"]
    assert check(context, counter).status == "ready"


def test_same_request_reuses_result_but_changed_content_does_not(counter):
    context = ctx(SystemMessage(content="稳定"), HumanMessage(content="材料"))
    first = check(context, counter)
    count = counter.count_text.call_count
    assert check(context, counter, previous=first) is first
    assert counter.count_text.call_count == count
    changed = ctx(*context.messages, HumanMessage(content="新要求"))
    second = check(changed, counter, previous=first)
    assert second.request_key != first.request_key
    assert counter.count_text.call_count > count
    count = counter.count_text.call_count
    different_budget = check_input(
        context=changed,
        counter=counter,
        budget_settings=BudgetSettings(6000),
        previous=second,
    )
    assert different_budget.conditions_key != second.conditions_key
    assert counter.count_text.call_count > count


def test_exact_threshold_is_over_budget(counter):
    context = ctx(SystemMessage(content="固定要求"), HumanMessage(content="材料"))
    total = check(context, counter).estimate.total
    exact = check_input(
        context=context, counter=counter, budget_settings=BudgetSettings(total * 2, 0.5)
    )
    below = check_input(
        context=context,
        counter=counter,
        budget_settings=BudgetSettings(total * 2 + 2, 0.5),
    )
    assert exact.status == "over_budget" and below.status == "ready"


def test_usage_plus_only_incremental_visible_reply_and_tools(counter):
    context = ctx(SystemMessage(content="原政策"), HumanMessage(content="原文"))
    first = check(context, counter)
    response = AIMessage(
        content="",
        tool_calls=[call("a"), call("b")],
        usage_metadata={
            "input_tokens": 123,
            "output_tokens": 5000,
            "total_tokens": 5123,
        },
        additional_kwargs={"reasoning_content": "不回传的思考" * 100},
    )
    base = usage_baseline_from_response(context, first, response, confirmed=True)
    added = [
        response,
        ToolMessage(content="甲", tool_call_id="a"),
        ToolMessage(content="乙", tool_call_id="b"),
        HumanMessage(content="提醒"),
    ]
    counter.count_text.reset_mock()
    result = check(ctx(*context.messages, *added), counter, usage_baseline=base)
    assert counter.count_text.call_count == len(added)
    assert result.estimate.method.startswith("usage+")
    assert (
        result.estimate.total
        == 123 + result.estimate.local_total - first.estimate.local_total
    )
    counted = str(counter.count_text.call_args_list)
    assert "原政策" not in counted and "不回传的思考" not in counted
    # 同一下一次请求再次检查，仍复用；缓存输入不能被再次叠加。
    again = check(
        ctx(*context.messages, *added), counter, usage_baseline=base, previous=result
    )
    assert again is result
    assert usage_baseline_from_response(context, first, response) is None
    assert (
        usage_baseline_from_response(
            context, first, AIMessage(content="x"), confirmed=True
        )
        is None
    )


def test_deepseek_replayed_reasoning_is_counted_once_and_unbinding_recounts(counter):
    protocol = ChatProtocol("deepseek", "deepseek-v4-flash", True)
    context = InputContext(
        (HumanMessage(content="核验"),),
        {"tools": [{"type": "function", "function": {"name": "search"}}]},
        protocol,
    )
    first = check(context, counter)
    reply = AIMessage(
        content="",
        tool_calls=[call("a")],
        additional_kwargs={"reasoning_content": "需要核实参数" * 30},
        usage_metadata={
            "input_tokens": 200,
            "output_tokens": 5000,
            "total_tokens": 5200,
        },
    )
    baseline = usage_baseline_from_response(context, first, reply, confirmed=True)
    continuation = replace(
        context,
        messages=(
            *context.messages,
            reply,
            ToolMessage(content="已查明", tool_call_id="a"),
        ),
    )
    counter.count_text.reset_mock()
    second = check(continuation, counter, usage_baseline=baseline)
    assert second.estimate.method.startswith("usage+")
    assert (
        second.estimate.total
        == 200 + second.estimate.local_total - first.estimate.local_total
    )
    counted = str(counter.count_text.call_args_list)
    assert counted.count("reasoning_content") == 1 and "需要核实参数" in counted
    assert second.estimate.total < 5000  # 未盲加 completion_tokens。
    assert (
        check(continuation, counter, usage_baseline=baseline, previous=second) is second
    )
    final = replace(continuation, bindings={})
    counter.count_text.reset_mock()
    assert check(final, counter, usage_baseline=baseline).estimate.method.startswith(
        "local:"
    )
    assert "reasoning_content" not in str(counter.count_text.call_args_list)
    assert reply.additional_kwargs["reasoning_content"]  # 没有删改历史对象。


def test_platform_and_thinking_changes_invalidate_usage_even_without_history(counter):
    context = InputContext(
        (HumanMessage(content="输入未变"),),
        protocol=ChatProtocol("deepseek", "alias", False),
    )
    first = check(context, counter)
    reply = AIMessage(
        content="完成",
        usage_metadata={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
    )
    baseline = usage_baseline_from_response(context, first, reply, confirmed=True)
    for protocol in (
        ChatProtocol("bailian", "alias", False),
        ChatProtocol("deepseek", "alias", True),
    ):
        changed = check(
            replace(context, protocol=protocol),
            counter,
            previous=first,
            usage_baseline=baseline,
        )
        assert changed.conditions_key != first.conditions_key
        assert changed.estimate.method.startswith("local:")


@pytest.mark.parametrize("change", ["system", "tools", "model", "counter"])
def test_rewritten_prefix_or_conditions_discard_usage(counter, change):
    original = ctx(SystemMessage(content="s"), HumanMessage(content="h"))
    result = check(original, counter)
    response = AIMessage(
        content="ok",
        usage_metadata={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
    )
    base = usage_baseline_from_response(original, result, response, confirmed=True)
    messages = list(original.messages)
    bindings, settings = {}, BudgetSettings(5000, model_key="test")
    if change == "system":
        messages[0] = SystemMessage(content="新视图")
    elif change == "tools":
        bindings = {"response_format": {"type": "json_object"}}
    elif change == "model":
        settings = replace(settings, model_key="new")
    else:
        counter = replace(counter, name="new-version")
    result = check_input(
        context=ctx(*messages, bindings=bindings),
        budget_settings=settings,
        counter=counter,
        usage_baseline=base,
    )
    assert result.estimate.method.startswith("local:")


@pytest.mark.parametrize(
    "messages",
    [
        [AIMessage(content="", tool_calls=[call("a")])],
        [ToolMessage(content="x", tool_call_id="missing")],
        [
            AIMessage(content="", tool_calls=[call("a"), call("b")]),
            ToolMessage(content="x", tool_call_id="a"),
            HumanMessage(content="插入提醒"),
            ToolMessage(content="y", tool_call_id="b"),
        ],
    ],
)
def test_unclosed_or_orphan_tool_messages_block(counter, messages):
    result = check(ctx(*messages), counter)
    assert result.status == "blocked" and result.reason
    counter.count_text.assert_not_called()


def test_images_count_per_occurrence_not_base64_or_path(counter):
    small = check(ctx(image_message(100)), counter)
    huge = check(ctx(image_message(10000)), counter)
    assert small.estimate.total == huge.estimate.total
    double = check(ctx(image_message(), image_message()), counter)
    assert double.estimate.total == 2 * (small.estimate.total - 3) + 3
    path_only = check(
        ctx(HumanMessage(content="![图](file-1/images/1.png)")),
        replace(counter, image_tokens_per_image=None),
    )
    assert path_only.status == "ready"
    assert (
        check(
            ctx(image_message()), replace(counter, image_tokens_per_image=None)
        ).status
        == "blocked"
    )
    assert "AAAAAAA" not in str(counter.count_text.call_args_list)


def test_tokenizer_unknown_is_visible_utf8_fallback(caplog):
    counter = get_token_counter("private-unknown", image_tokens_per_image=100)
    assert counter.count_text("中文") == 6
    assert counter.degradation_reason and "降级" in caplog.text
    assert counter.count_image() == 100


def test_structural_batches_are_contiguous_and_atomic(counter):
    units = ["A" * 500, "TABLE" * 140, "C" * 200]

    def builder(start, end):
        return ctx(
            SystemMessage(content="政策"),
            HumanMessage(content="\n".join(units[start:end])),
        )

    settings = BudgetSettings(1200)
    first = plan_document_input(
        units=units,
        cursor=0,
        build_context=builder,
        counter=counter,
        budget_settings=settings,
    )
    assert (first.start, first.end, first.status) == (0, 1, "ready")
    second = plan_document_input(
        units=units,
        cursor=first.end,
        build_context=builder,
        counter=counter,
        budget_settings=settings,
    )
    assert (second.start, second.end) == (1, 3)
    assert "TABLE" * 140 in second.context.messages[-1].content
    calls = counter.count_text.call_count
    assert (
        check_input(
            context=second.context,
            counter=counter,
            budget_settings=settings,
            previous=second.decision,
        )
        is second.decision
    )
    assert counter.count_text.call_count == calls
    assert (
        plan_document_input(
            units=units,
            cursor=3,
            build_context=builder,
            counter=counter,
            budget_settings=settings,
        ).status
        == "done"
    )
    blocked = plan_document_input(
        units=units,
        cursor=1,
        build_context=builder,
        counter=counter,
        budget_settings=BudgetSettings(300),
    )
    assert blocked.status == "blocked" and blocked.start == blocked.end == 1


def test_empty_document_is_not_success_and_fixed_input_can_block(counter):
    kwargs = dict(
        build_context=lambda a, b: ctx(SystemMessage(content="X" * 300)),
        counter=counter,
        budget_settings=BudgetSettings(100),
    )
    assert plan_document_input(units=[], cursor=0, **kwargs).status == "blocked"
    assert plan_document_input(units=["tiny"], cursor=0, **kwargs).status == "blocked"


def controller(counter, *, capacity=10000, ratio=0.9):
    settings = SimpleNamespace(
        context_window_tokens=capacity,
        compaction_trigger_ratio=ratio,
        base_url="https://mock.test/v1",
        model="mock",
        platform="openai",
        enable_thinking=False,
    )
    return InputController(settings, counter, ModelCapabilities(10000, 2000, 100, True))


@pytest.mark.asyncio
async def test_generic_compaction_without_file_cursor_preserves_human_order(counter):
    runtime = controller(counter, ratio=0.08)
    original = ctx(
        SystemMessage(content="稳定政策"),
        HumanMessage(content="原要求"),
        AIMessage(content="已有进展"),
        HumanMessage(content="最新补充要求"),
        image_message(),
    )
    before = [m.model_dump() for m in original.messages]
    invoke = AsyncMock(
        return_value=AIMessage(content="完成：已查明一项。待办：按最新要求检查图像。")
    )
    result = await compact_context(
        context=original, constraints="保留补充要求", controller=runtime, invoke=invoke
    )
    sent, allowance = invoke.call_args.args
    assert not sent.bindings and allowance > 0
    transcript = sent.messages[-1].content
    assert transcript.index("原要求") < transcript.index("最新补充要求")
    assert "data:image" not in transcript and "file-1/images/1.png" in transcript
    assert (
        result.input_tokens >= runtime.budget.threshold
    )  # 摘要可越软阈值，不能越硬窗口
    assert [m.model_dump() for m in original.messages] == before
    assert runtime.baseline is None and runtime.previous is None
    rebuilt = ctx(
        SystemMessage(content="稳定政策"),
        HumanMessage(content="原要求\n" + result.summary),
    )
    assert runtime.check(rebuilt).status == "ready"


@pytest.mark.asyncio
async def test_compaction_keeps_facts_not_reasoning_or_invented_round_control(counter):
    runtime = controller(counter)
    runtime.settings.platform = "deepseek"
    runtime.settings.enable_thinking = True
    original = InputContext(
        (
            HumanMessage(content="按当前资料核实"),
            AIMessage(
                content="",
                tool_calls=[call("a")],
                additional_kwargs={"reasoning_content": "隐藏推理不应交接"},
            ),
            ToolMessage(
                content="file-1：90日，从截止日计算；投标方声明尚未核实，不能当成通过。",
                tool_call_id="a",
            ),
        ),
        {"tools": [{"type": "function", "function": {"name": "search"}}]},
        ChatProtocol.from_settings(runtime.settings),
    )
    invoke = AsyncMock(
        return_value=AIMessage(
            content="待核实：file-1 的90日及起算要求，尚未得出合规结论。"
        )
    )
    await compact_context(
        context=original,
        constraints="原文会重送；待办不可丢。",
        controller=runtime,
        invoke=invoke,
    )
    sent = invoke.call_args.args[0]
    assert not sent.bindings
    assert "隐藏推理不应交接" not in str(sent.messages)
    assert "不能当成通过" in sent.messages[-1].content
    assert "不要根据转录条数推算轮次" in sent.messages[0].content
    assert "固定压缩率" in sent.messages[0].content


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [
        AIMessage(content=""),
        AIMessage(content="半截", response_metadata={"finish_reason": "length"}),
        AIMessage(content="", tool_calls=[call("wrong")]),
    ],
)
async def test_bad_compaction_is_not_retried(counter, response):
    invoke = AsyncMock(return_value=response)
    with pytest.raises(InputBudgetError):
        await compact_context(
            context=ctx(HumanMessage(content="材料"), AIMessage(content="进展")),
            constraints="保持事实",
            controller=controller(counter),
            invoke=invoke,
        )
    assert invoke.await_count == 1


@pytest.mark.asyncio
async def test_compaction_request_too_large_never_sent(counter):
    invoke = AsyncMock()
    with pytest.raises(InputBudgetError):
        await compact_context(
            context=ctx(HumanMessage(content="X" * 10000)),
            constraints="",
            controller=controller(counter),
            invoke=invoke,
        )
    invoke.assert_not_called()


def test_output_allowance_respects_capacity_and_service_limit(counter):
    runtime = controller(counter)
    caps = runtime.capabilities
    amount = output_allowance(runtime.settings, 8900, caps)
    assert amount == 900 and 8900 + amount < 10000
    assert output_allowance(runtime.settings, 9500, caps, compaction=True) == 400
    assert (
        output_allowance(runtime.settings, 100, replace(caps, max_output_tokens=25))
        == 25
    )
    with pytest.raises(ValueError):
        output_allowance(runtime.settings, 9950, caps, compaction=True)
    with pytest.raises(ValueError, match="服务输入上限"):
        output_allowance(
            runtime.settings,
            9200,
            replace(caps, max_input_tokens=9100),
            compaction=True,
        )


@pytest.mark.parametrize(
    "platform,url,model,capacity,output",
    [
        (
            "deepseek",
            "https://api.deepseek.com",
            "deepseek-v4-flash",
            1_000_000,
            393216,
        ),
        (
            "deepseek",
            "https://api.deepseek.com/v1/",
            "deepseek-v4-flash",
            1_000_000,
            393216,
        ),
        (
            "bailian",
            "https://dashscope.aliyuncs.com/compatible-mode/v1",
            "qwen3.6-plus",
            1_000_000,
            65536,
        ),
        ("openai", "https://api.openai.com/v1", "gpt-5.1", 400_000, 128000),
    ],
)
def test_registered_model_capabilities_are_platform_and_endpoint_specific(
    platform, url, model, capacity, output
):
    from app.llm.model_capabilities import get_model_capabilities

    settings = SimpleNamespace(
        platform=platform, base_url=url, model=model, context_window_tokens=capacity
    )
    assert get_model_capabilities(settings).max_output_tokens == output
    settings.platform = "unregistered"
    with pytest.raises(ValueError, match="尚未登记"):
        get_model_capabilities(settings)
    settings.platform = platform
    settings.context_window_tokens += 1
    with pytest.raises(ValueError, match="超过"):
        get_model_capabilities(settings)


def test_service_capabilities_do_not_guess_third_party_gateway():
    from app.llm.model_capabilities import get_model_capabilities

    settings = SimpleNamespace(
        base_url="https://unverified.example/v1",
        model="deepseek-flash",
        platform="deepseek",
        context_window_tokens=10000,
    )
    with pytest.raises(ValueError, match="尚未登记"):
        get_model_capabilities(settings)


def test_missing_tokenizer_falls_back_without_network(monkeypatch, tmp_path, caplog):
    from app.llm import token_counter

    token_counter.build_deepseek_counter.cache_clear()
    with monkeypatch.context() as patch:
        patch.setattr(token_counter, "TOKENIZER_ROOT", tmp_path)
        counter = token_counter.get_token_counter("DeepSeek")
        assert counter.name == "utf8-bytes-v1"
        assert counter.count_text("中文") == 6 and "缺少本地" in caplog.text
    token_counter.build_deepseek_counter.cache_clear()


def test_parent_child_and_summary_do_not_share_usage_baselines(counter):
    parent, child = controller(counter), controller(counter)
    context = ctx(SystemMessage(content="稳定要求"), HumanMessage(content="任务"))
    parent.check(context)
    parent.observe(
        context,
        AIMessage(
            content="ok",
            usage_metadata={
                "input_tokens": 500,
                "output_tokens": 10,
                "total_tokens": 510,
            },
        ),
    )
    assert parent.baseline is not None and child.baseline is None
    assert child.check(context).estimate.method.startswith("local:")
    parent.reset_history()
    assert parent.baseline is None and parent.previous is None
