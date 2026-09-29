"""项目路由：完整结构前缀、两字段 JSON、一次有界格式修复；不生成摘要。"""

import json
from pathlib import Path

from langchain_core.messages import HumanMessage, SystemMessage

from app.audit.session import make_controller
from app.llm.agent_tracing import invoke_agent_model
from app.llm.context_compaction import InputBudgetError
from app.llm.errors import InvalidStructuredOutput
from app.llm.factory import budgeted_generation_options
from app.llm.input_budget import InputContext, plan_document_input
from app.llm.model_diagnostics import budget_diagnostics
from app.llm.platforms import ChatProtocol
from app.llm.structured_output import extract_json_value
from app.models.documents import BidDocumentSummary, BidRouteDecision
from app.parsers.assets import atomic_write
from app.parsers.reading import build_document_units, render_document_range
from app.prompts.loader import render_prompt


async def summarize_and_route_bid_document(
    parsed, rule_set, model, model_settings, *, limiter=None, trace_context=None
):
    from app.audit.concurrency import model_limiter

    limiter = limiter or model_limiter(3)
    controller = make_controller(model_settings)
    units = build_document_units(parsed)

    def build(start, end):
        return InputContext(
            (
                SystemMessage(
                    content=render_prompt(
                        "bid_document_summary",
                        bid_route_schema=json.dumps(
                            BidRouteDecision.model_json_schema(), ensure_ascii=False
                        ),
                    )
                ),
                HumanMessage(
                    content=render_prompt(
                        "bid_document_summary_input",
                        file_identity=f"文件ID：{parsed.document.file_id}\n文件名：{parsed.document.file_name}",
                        project_profile=rule_set.project_profile,
                        reading_scope=f"从开头第1至{end}个完整结构，共{len(units)}个；"
                        + (
                            "已提供全文。"
                            if end == len(units)
                            else "仅提供前部，后部未读。"
                        ),
                        document_context=render_document_range(
                            parsed, units, start, end
                        ),
                    )
                ),
            ),
            protocol=ChatProtocol.from_settings(model_settings),
        )

    batch = plan_document_input(
        units=units,
        cursor=0,
        build_context=build,
        budget_settings=controller.budget,
        counter=controller.counter,
    )
    if batch.status != "ready":
        raise InputBudgetError(batch.blocked_reason)
    context = batch.context
    for attempt in range(2):
        tokens = controller.output_tokens(context)
        async with limiter:
            response = await invoke_agent_model(
                model,
                list(context.messages),
                [],
                trace_context or {},
                trace_name="bid_route",
                diagnostic_context={
                    **budget_diagnostics(controller),
                    "file_id": parsed.document.file_id,
                    "project_id": rule_set.project_id,
                    "attempt": attempt + 1,
                },
                **budgeted_generation_options(model_settings, tokens),
            )
        controller.observe(context, response)
        try:
            if response.tool_calls or getattr(response, "invalid_tool_calls", None):
                raise ValueError("路由不允许工具调用")
            if response.response_metadata.get("finish_reason") not in (None, "stop"):
                raise ValueError("路由输出未正常结束")
            decision = BidRouteDecision.model_validate(
                extract_json_value(response.content)
            )
            return BidDocumentSummary(
                file_id=parsed.document.file_id,
                reading_end=batch.end,
                total_units=len(units),
                **decision.model_dump(),
            )
        except (ValueError, TypeError, InvalidStructuredOutput) as exc:
            if attempt:
                raise ValueError(f"路由 JSON 修复失败：{exc}") from exc
            # 不复送截断的长响应或活跃 tool_calls，反馈只含校验问题。
            context = InputContext(
                (
                    *batch.context.messages,
                    HumanMessage(
                        content=f"上次输出未通过校验：{str(exc)[:1000]}。请仅输出 match_status、match_reason 的合法 JSON。"
                    ),
                ),
                protocol=batch.context.protocol,
            )
    raise RuntimeError("路由未产生结果")


def summary_path(file_id, settings) -> Path:
    return settings.storage.data_root / "parsed" / "bid_routes" / f"{file_id}.json"


def write_bid_summary(summary, settings):
    path = summary_path(summary.file_id, settings)
    atomic_write(path, summary.model_dump_json(indent=2).encode("utf-8"))
    return path


def load_bid_summary(file_id, settings):
    return BidDocumentSummary.model_validate_json(
        summary_path(file_id, settings).read_text("utf-8")
    )
