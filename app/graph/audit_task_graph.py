"""文件级并发编排；每个文件/批次独立图调用，导出是服务函数而非图节点。"""

import asyncio
import logging
from uuid import uuid4

from langsmith import traceable
from langsmith.run_helpers import get_tracing_context
from langsmith.utils import tracing_is_enabled

from app.agents.bid_agent import build_bid_agent_messages
from app.audit.concurrency import model_limiter
from app.audit.context import scope_for, verify_indexes
from app.audit.session import (
    build_session_graph,
    make_controller,
    prepare_request,
    run_session,
)
from app.audit.sources import SourceLedger
from app.graph.state_updates import file_result_from_report
from app.llm.context_compaction import InputBudgetError
from app.llm.input_budget import plan_document_input
from app.models.results import AuditResult
from app.models.rules import RuleReportItem, present_rule_for_model
from app.parsers.assets import atomic_write
from app.parsers.reading import build_document_units
from app.reports.markdown_report import ReportDocument
from app.retrieval.service import RetrievalService
from app.tools.bid_tools import build_evidence_review_tool
from app.tools.common_tools import CommonTool
from app.tools.report_tools import report_tool

logger = logging.getLogger(__name__)


def build_audit_graph(**kwargs):
    return build_session_graph(role="bid_agent", **kwargs)


class LangGraphAuditExecutor:
    def __init__(self, settings, models, event_repository=None):
        self.settings, self.models, self.events = settings, models, event_repository

    async def run(self, context):
        context = context.model_copy(deep=True)
        indexes = verify_indexes(
            context.parsed_documents, self.settings, context.index_scope_overrides
        )
        retrieval = RetrievalService(
            self.models.embedding, self.models.rerank, self.settings, indexes=indexes
        )
        limiter = model_limiter(self.settings.audit.model_concurrency)
        files = asyncio.Semaphore(self.settings.audit.file_concurrency)

        async def execute(document):
            async with files:
                return await self._run_file(context, document, retrieval, limiter)

        outcomes = await asyncio.gather(
            *(execute(file) for file in context.bid_files), return_exceptions=True
        )
        results = {item.file.file_id: item for item in context.routed_file_results}
        errors = dict(context.file_errors)
        for file, value in zip(context.bid_files, outcomes, strict=True):
            if isinstance(value, asyncio.CancelledError):
                raise value
            if isinstance(value, Exception):
                logger.error(
                    "文件审核失败 file_id=%s",
                    file.file_id,
                    exc_info=(type(value), value, value.__traceback__),
                )
                errors[file.file_id] = f"{type(value).__name__}: {value}"
            else:
                results[file.file_id] = value
        return AuditResult(
            task_id=context.task_id,
            project_id=context.project_id,
            bidder_id=context.bidder_id,
            evidence_library=context.evidence_library,
            rules=[
                RuleReportItem.model_validate(present_rule_for_model(rule).model_dump())
                for rule in context.rule_set.rules
            ],
            rule_source_files=[
                file.model_dump() for file in context.rule_set.source_files
            ],
            file_results=[results[key] for key in context.file_order if key in results],
            file_errors=errors,
        )

    @traceable(
        name="bid_audit_agent",
        process_inputs=lambda values: {
            "task_id": values["context"].task_id,
            "file_id": values["document"].file_id,
        },
        process_outputs=lambda value: (
            value.model_dump(mode="json") if value is not None else None
        ),
    )
    async def _run_file(self, context, document, retrieval, limiter):
        parsed = context.parsed_documents[document.file_id]
        units = build_document_units(parsed)
        main_parsed = {
            key: value
            for key, value in context.parsed_documents.items()
            if value.document.data_role == "rule_source" or key == document.file_id
        }
        ledger = SourceLedger(context.parsed_documents)
        directory = self.settings.storage.data_root / "outputs" / context.task_id
        report = ReportDocument(
            directory / "drafts" / f"{document.file_id}.md", context.rule_set, ledger
        )
        records, stats = [], {}
        trace = get_tracing_context()
        trace["enabled"] = tracing_is_enabled(trace)
        trace["metadata"] = {
            **(trace.get("metadata") or {}),
            "task_id": context.task_id,
            "file_id": document.file_id,
            "thread_id": f"{context.task_id}-{document.file_id}-{uuid4().hex}",
        }

        def event(actor):
            def emit(step, final):
                if self.events:
                    self.events.append(
                        context.task_id,
                        actor=actor,
                        event_type="agent_turn_started",
                        title="主 Agent 审核当前投标文件"
                        if actor == "main_agent"
                        else "资料核验子 Agent 分析资料",
                        detail={
                            "file_id": document.file_id,
                            "step": step,
                            "force_finalize": final,
                        },
                    )

            return emit

        tools = CommonTool(
            parsed_documents=main_parsed,
            settings=self.settings,
            retrieval=retrieval,
            file_filters={
                key: scope_for(value.document, context.index_scope_overrides)
                for key, value in main_parsed.items()
            },
            default_file_ids=[document.file_id],
            current_file_id=document.file_id,
            on_chunk_read=ledger.chunk_read,
            on_image_read=ledger.image_read,
            strict_errors=True,
        ).tools()
        tools.extend(
            [
                build_evidence_review_tool(
                    context=context,
                    settings=self.settings,
                    model=self.models.evidence_chat,
                    retrieval=retrieval,
                    ledger=ledger,
                    limiter=limiter,
                    trace_context=trace,
                    records=records,
                    events=event("evidence_agent"),
                ),
                report_tool(report),
            ]
        )
        controller = make_controller(self.settings.bid_chat_model)
        cursor, batch_number = 0, 0

        def checkpoint(error=None):
            import json

            value = {
                "file_id": document.file_id,
                "next_unit_index": cursor,
                "total_units": len(units),
                "batch_index": batch_number,
                "stats": stats,
                "error": error,
                "sources": {
                    key: value.model_dump(mode="json")
                    for key, value in ledger.sources.items()
                },
                "evidence_reviews": records,
            }
            atomic_write(
                directory / "drafts" / f"{document.file_id}.state.json",
                json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8"),
            )

        try:
            while cursor < len(units):
                batch_number += 1
                report.start_batch(batch_number)

                def context_for(start, end):
                    return prepare_request(
                        {
                            "messages": build_bid_agent_messages(
                                context, parsed, units, start, end, report
                            ),
                            "model_calls": 0,
                        },
                        tools=tools,
                        max_steps=self.settings.audit.bid_agent_max_steps,
                        settings=self.settings.bid_chat_model,
                        role="bid_agent",
                        missing="、".join(report.missing()),
                    )

                batch = plan_document_input(
                    units=units,
                    cursor=cursor,
                    build_context=context_for,
                    budget_settings=controller.budget,
                    counter=controller.counter,
                )
                if batch.status != "ready":
                    raise InputBudgetError(batch.blocked_reason)
                trace["metadata"]["batch_index"] = batch_number
                trace["metadata"]["reading_range"] = [batch.start, batch.end]
                content = await run_session(
                    model=self.models.bid_chat,
                    tools=tools,
                    settings=self.settings.bid_chat_model,
                    max_steps=self.settings.audit.bid_agent_max_steps,
                    role="bid_agent",
                    limiter=limiter,
                    trace_context=trace,
                    build_messages=lambda handoff, batch=batch: (
                        build_bid_agent_messages(
                            context,
                            parsed,
                            units,
                            batch.start,
                            batch.end,
                            report,
                            handoff,
                        )
                    ),
                    report=report,
                    stats=stats,
                    events=event("main_agent"),
                )
                if report.write_error or content.rstrip("。.!！") != "处理完毕":
                    raise RuntimeError(report.write_error or f"当前批未完成：{content}")
                cursor = batch.end
                checkpoint()
            decision = report.finish()
            result = file_result_from_report(
                document, context.bid_summaries[document.file_id], decision, ledger
            )
            atomic_write(
                directory / f"{document.file_id}_result.json",
                result.model_dump_json(indent=2).encode("utf-8"),
            )
            return result
        except Exception as exc:
            checkpoint(str(exc))
            raise
