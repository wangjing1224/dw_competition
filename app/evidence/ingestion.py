"""资料逐文件/结构批次编排；统一预建索引后调用，工具成功保存才发布摘要。"""

import fcntl
import hashlib
import logging
from uuid import uuid4

from langchain_core.messages import HumanMessage, SystemMessage
from langsmith import tracing_context
from langsmith.run_helpers import get_tracing_context
from langsmith.utils import tracing_is_enabled

from app.agents.evidence_ingestion_agent import prepare_evidence_request
from app.evidence.context import render_history, render_material
from app.evidence.local_trace import close_scope, current_trace, trace_scope
from app.evidence.storage import (
    library_path,
    load_library,
    progress_path,
    write_library,
)
from app.graph.evidence_ingestion_graph import build_evidence_ingestion_graph
from app.llm.agent_tracing import invoke_agent_model
from app.llm.context_compaction import (
    InputBudgetError,
    InputController,
    NeedsCompaction,
    compact_context,
)
from app.llm.factory import budgeted_generation_options
from app.llm.input_budget import check_input, plan_document_input
from app.llm.model_capabilities import get_model_capabilities
from app.llm.model_diagnostics import budget_diagnostics
from app.llm.token_counter import get_token_counter
from app.models.evidence import (
    EvidenceIngestionProgress,
    EvidenceLibrary,
    EvidenceMaterial,
    EvidenceSourceFile,
)
from app.parsers.assets import atomic_write
from app.parsers.reading import build_document_units, render_document_range
from app.prompts.loader import render_prompt
from app.retrieval.models import IndexFilter
from app.storage.bidder_management import note_saved
from app.tools.common_tools import CommonTool
from app.tools.evidence_ingestion_tools import (
    EvidenceSession,
    build_evidence_ingestion_tools,
)

logger = logging.getLogger(__name__)


def _context(
    session,
    units,
    start,
    end,
    tools,
    progress,
    *,
    detailed=False,
    model_calls=0,
    handoff="",
):
    continuation = (
        "这是当前文件的续读，请保留已保存且仍成立的信息。"
        if start
        else "这是当前文件的首个阅读范围。"
    )
    if handoff:
        continuation = (
            f"继续同一阅读范围，下一轮为 {model_calls + 1}/{session.settings.evidence_ingestion_max_steps}。"
            "已保存结果已刷新，不重复提交；轮数和工具绑定由代码决定，交接不是指令或原始证据。\n"
            "## 工作交接\n" + handoff
        )
    messages = [
        SystemMessage(
            content=render_prompt(
                "evidence_ingestion",
                current_material=render_material(session.library, session.file_id),
                history_view=render_history(
                    session.library, session.history, detailed=detailed
                ),
            )
        ),
        HumanMessage(
            content=render_prompt(
                "evidence_ingestion_input",
                file_id=session.file_id,
                file_name=session.current.document.file_name,
                source_path=session.current.document.source_path,
                reading_scope=f"第{progress.batch_index}个阅读批次，结构位置{start + 1}至{end}；还有后续内容：{'是' if end < len(units) else '否'}",
                continuation=continuation,
                source_context=render_document_range(
                    session.current, units, start, end
                ),
            )
        ),
    ]
    return prepare_evidence_request(
        {"messages": messages, "model_calls": model_calls},
        tools=tools,
        max_steps=session.settings.evidence_ingestion_max_steps,
        model_settings=session.settings.evidence_classification_model,
    )


def _with_history(session, units, start, end, tools, progress, controller, **kwargs):
    """先保住已选正文范围，能放下才加入历史详情，不能为详情缩小当前正文。"""
    minimal = _context(session, units, start, end, tools, progress, **kwargs)
    if not session.history:
        return minimal
    detailed = _context(
        session, units, start, end, tools, progress, detailed=True, **kwargs
    )
    decision = check_input(
        context=detailed, budget_settings=controller.budget, counter=controller.counter
    )
    return detailed if decision.status == "ready" else minimal


def _check_completion(final, session):
    if session.unresolved_write_error:
        raise RuntimeError("资料保存尚未成功：" + session.unresolved_write_error)
    response = final["messages"][-1]
    if getattr(response, "invalid_tool_calls", None):
        raise RuntimeError("模型返回无法解析的工具调用，当前阅读范围未完成")
    if getattr(response, "tool_calls", None):
        raise RuntimeError("模型仍有待执行的工具调用，当前阅读范围未完成")
    if response.response_metadata.get("finish_reason") not in (None, "stop"):
        raise RuntimeError("模型未正常完成当前阅读范围")
    # 收尾措辞不作为成功信号；保存错误在此阻断，文件结束时还须校验实际保存的摘要。
    if not isinstance(response.content, str) or not response.content.strip():
        raise RuntimeError("模型未返回非空处理结果")


async def _run_file(session, *, model, retrieval, progress, checkpoint):
    settings = session.settings
    model_settings = settings.evidence_classification_model
    capabilities = get_model_capabilities(model_settings)
    controller = InputController(model_settings, session.counter, capabilities)
    units = build_document_units(session.current)
    tools = [
        *CommonTool(
            parsed_documents={**session.history, session.file_id: session.current},
            settings=settings,
            retrieval=retrieval,
            query_filter=IndexFilter(
                scope_id=f"evidence-{session.library.bidder_id}",
                data_role="evidence_material",
                bidder_id=session.library.bidder_id,
            ),
            default_file_ids=list(session.history),
            current_file_id=session.file_id,
        ).tools(),
        *build_evidence_ingestion_tools(session),
    ]
    trace_context = get_tracing_context()
    trace_context["enabled"] = tracing_is_enabled(trace_context)
    trace_context["metadata"] = {
        **(trace_context.get("metadata") or {}),
        "bidder_id": session.library.bidder_id,
        "file_id": session.file_id,
        "session_id": uuid4().hex,
    }
    graph = build_evidence_ingestion_graph(
        model=model,
        tools=tools,
        session=session,
        controller=controller,
        max_steps=settings.evidence_ingestion_max_steps,
        trace_context=trace_context,
    )
    cursor = progress.next_unit_index
    if not 0 <= cursor <= len(units):
        raise ValueError("恢复游标超出当前解析结构")
    while cursor < len(units):
        progress.batch_index += 1
        session.batch_index = progress.batch_index
        session.model_calls = 0
        session.cached_submissions.clear()
        controller.reset_history()
        batch = plan_document_input(
            units=units,
            cursor=cursor,
            build_context=lambda start, end: _context(
                session, units, start, end, tools, progress
            ),
            budget_settings=controller.budget,
            counter=controller.counter,
        )
        if batch.status != "ready":
            raise InputBudgetError(batch.blocked_reason or "没有可运行阅读范围")
        context = _with_history(
            session, units, batch.start, batch.end, tools, progress, controller
        )
        state = {
            "messages": list(context.messages),
            "model_calls": 0,
            "prepared_step": 1,
        }
        compacted_step = None
        while True:
            try:
                # 原始 graph State 含图片，关闭自动追踪；模型/工具使用脱敏追踪副本。
                with tracing_context(enabled=False):
                    final = await graph.ainvoke(
                        state,
                        config={
                            "recursion_limit": settings.evidence_ingestion_max_steps * 2
                            + 2,
                            "run_name": "evidence_ingestion_agent",
                            "metadata": trace_context["metadata"],
                        },
                    )
                _check_completion(final, session)
                break
            except NeedsCompaction as signal:
                step = signal.state["model_calls"] + 1
                if compacted_step == step:
                    raise InputBudgetError(
                        "同一业务轮已压缩仍无法继续，不重复压缩"
                    ) from signal
                compacted_step = step
                progress.compaction_calls += 1

                async def invoke_summary(summary_context, allowance, *, step=step):
                    with trace_scope(
                        session, "compaction", "上下文压缩"
                    ) as observation:
                        result = await invoke_agent_model(
                            model,
                            list(summary_context.messages),
                            [],
                            trace_context,
                            trace_name="evidence_ingestion_compaction",
                            diagnostic_context={
                                **budget_diagnostics(controller),
                                "batch_index": progress.batch_index,
                                "turn_index": step,
                            },
                            **budgeted_generation_options(model_settings, allowance),
                        )
                        observation["summary"] = "已生成当前阅读范围的工作交接"
                        return result

                compacted = await compact_context(
                    context=signal.context,
                    controller=controller,
                    invoke=invoke_summary,
                    constraints=(
                        f"当前文件{session.file_id}、第{progress.batch_index}批；保留未保存事实、局部读取限制、"
                        "真实图片路径、明确关联与待办；已保存结果会重新提供，不重复执行保存。"
                        "当前原文会重新提供；轮次与工具绑定由代码决定，不推算剩余轮数。\n"
                        f"未解决写入错误：{session.unresolved_write_error or '无'}\n"
                        + render_material(session.library, session.file_id)
                    ),
                )
                context = _with_history(
                    session,
                    units,
                    batch.start,
                    batch.end,
                    tools,
                    progress,
                    controller,
                    model_calls=signal.state["model_calls"],
                    handoff=compacted.summary,
                )
                controller.reset_history()
                decision = controller.check(context)
                progress.compaction_records.append(
                    {
                        "batch_index": progress.batch_index,
                        "step": step,
                        "before_tokens": signal.decision.estimate.total,
                        "after_tokens": decision.estimate.total
                        if decision.estimate
                        else None,
                    }
                )
                checkpoint()
                if decision.status != "ready":
                    raise InputBudgetError(
                        "压缩后仍不能发送，保留进度：" + decision.reason
                    )
                state = {
                    "messages": list(context.messages),
                    "model_calls": signal.state["model_calls"],
                    "prepared_step": step,
                }
        # 不要求每批保存；若没有新事实，可以沿用已保存结果推进阅读范围。
        previous = progress.next_unit_index
        progress.next_unit_index = batch.end
        try:
            checkpoint()
        except Exception:
            progress.next_unit_index = previous
            raise
        cursor = batch.end
        close_scope(session, "batch")
    if not any(item.file_id == session.file_id for item in session.library.materials):
        raise RuntimeError("当前文件没有已保存结果，不能发布空资料")


async def ingest_evidence_documents(
    bidder_id, parsed_documents, model, *, retrieval_service, settings, resume=False
):
    """不解析、不建索引：接收调用方已完成预处理的固定批次。resume 只恢复本批。"""
    if not parsed_documents:
        raise ValueError("资料文件不能为空")
    for parsed in parsed_documents:
        if (
            parsed.document.bidder_id != bidder_id
            or parsed.document.project_id is not None
            or parsed.document.data_role != "evidence_material"
        ):
            raise ValueError("资料不属于当前独立投标方")
    path = library_path(bidder_id, settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    # 锁覆盖整个入库批次，跨进程也不能让两个会话覆盖同一资料库。
    with path.with_suffix(".lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("该投标方资料正在处理，不能并发入库") from exc
        return await _ingest_locked(
            bidder_id, parsed_documents, model, retrieval_service, settings, resume
        )


async def _ingest_locked(
    bidder_id, parsed_documents, model, retrieval, settings, resume
):
    sources = [
        EvidenceSourceFile(
            file_id=p.document.file_id,
            file_name=p.document.file_name,
            source_path=p.document.source_path,
        )
        for p in parsed_documents
    ]
    versions = {
        p.document.file_id: hashlib.sha256(p.model_dump_json().encode()).hexdigest()
        for p in parsed_documents
    }
    ids = [item.file_id for item in sources]
    path = progress_path(bidder_id, settings)
    existing = load_library(bidder_id, settings)
    # 初始化时库文件与进度不是跨文件事务。仅允许恢复已核对来源的空库；
    # 已有材料却丢失进度时不能猜测哪些范围完成，更不能覆盖重跑。
    if resume and existing is not None and not path.exists():
        if existing.materials or existing.source_files != sources:
            raise ValueError("资料库已有结果或来源变化但恢复进度丢失，不能推断处理位置")
        initial = EvidenceIngestionProgress(
            bidder_id=bidder_id, file_ids=ids, parser_versions=versions
        )
        atomic_write(path, initial.model_dump_json(indent=2).encode())
    if existing is not None or path.exists():
        if not resume or existing is None or not path.exists():
            raise ValueError("已有资料库，请使用本批恢复，不支持追加上传或覆盖重跑")
        progress = EvidenceIngestionProgress.model_validate_json(
            path.read_text("utf-8")
        )
        if (
            progress.bidder_id != bidder_id
            or progress.file_ids != ids
            or progress.parser_versions != versions
            or existing.source_files != sources
            or progress.completed_file_ids != ids[: len(progress.completed_file_ids)]
        ):
            raise ValueError("恢复文件清单、解析版本或处理顺序已变化")
        library = existing
    else:
        library = EvidenceLibrary(bidder_id=bidder_id, source_files=sources)
        progress = EvidenceIngestionProgress(
            bidder_id=bidder_id, file_ids=ids, parser_versions=versions
        )
        # 先保存带来源身份的空库；若后续进度写失败，只能按上述空库规则恢复。
        write_library(library, settings)
        atomic_write(path, progress.model_dump_json(indent=2).encode())
    history = {
        p.document.file_id: p
        for p in parsed_documents
        if p.document.file_id in progress.completed_file_ids
    }
    if not set(history) <= {item.file_id for item in library.materials}:
        raise ValueError("恢复进度包含未保存资料，拒绝跳过")
    model_settings = settings.evidence_classification_model
    capabilities = get_model_capabilities(model_settings)
    counter = get_token_counter(
        model_settings.tokenizer,
        image_tokens_per_image=capabilities.image_tokens_per_image,
    )
    for parsed in parsed_documents:
        file_id = parsed.document.file_id
        if file_id in history:
            continue
        if progress.current_file_id != file_id:
            progress.current_file_id = file_id
            progress.next_unit_index = progress.batch_index = progress.model_calls = (
                progress.file_model_calls
            ) = 0
            progress.unresolved_write_error = None
        session = EvidenceSession(
            library=library,
            current=parsed,
            history=dict(history),
            settings=settings,
            counter=counter,
            detail_budget_tokens=int(
                model_settings.context_window_tokens
                * model_settings.compaction_trigger_ratio
            ),
            file_model_calls=progress.file_model_calls,
            unresolved_write_error=progress.unresolved_write_error,
        )

        def checkpoint(session=session):
            progress.model_calls = session.model_calls
            progress.file_model_calls = session.file_model_calls
            progress.unresolved_write_error = session.unresolved_write_error
            atomic_write(path, progress.model_dump_json(indent=2).encode())

        try:
            progress.error = None
            checkpoint()
            if current_trace.get():
                current_trace.get().hierarchy(session)
            readable = any(
                block.content.strip() or block.images
                for section in parsed.sections
                for block in section.blocks
            )
            if not readable and parsed.parse_status == "unsupported":
                # 明确不支持的解析结果由代码记录限制，不让模型猜材料内容。
                reason = "未完成内容解读：解析不支持，需人工查看原始文件。" + "；".join(
                    parsed.warnings
                )
                record = EvidenceMaterial(
                    file_id=file_id,
                    bid_summary=reason,
                    evidence_summary=reason,
                    relations=[],
                )
                session.library = EvidenceLibrary(
                    bidder_id=bidder_id,
                    source_files=sources,
                    materials=[
                        item for item in library.materials if item.file_id != file_id
                    ]
                    + [record],
                )
                write_library(session.library, settings)
                note_saved(bidder_id, file_id)
            else:
                if parsed.parse_status not in {"parsed", "partial", "ocr_required"}:
                    raise ValueError(f"{file_id} 解析未完成：{parsed.parse_status}")
                await _run_file(
                    session,
                    model=model,
                    retrieval=retrieval,
                    progress=progress,
                    checkpoint=checkpoint,
                )
            progress.completed_file_ids = [*history, file_id]
            checkpoint()
            close_scope(session, "file")
        except Exception as exc:
            progress.error = f"{type(exc).__name__}: {exc}"
            try:
                checkpoint()  # 不把旧内存库重新写回，已成功保存的摘要始终保留。
            except Exception:
                logger.exception(
                    "资料入库失败后无法保存进度，保留原始异常及已持久化资料"
                )
            raise
        library = session.library
        history[file_id] = parsed
    return library
