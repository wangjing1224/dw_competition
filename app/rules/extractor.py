"""逐文件启动规则 Agent；每次提交原子落盘，后续文件只共享规则库。"""

import inspect
import logging
from collections.abc import Awaitable, Callable
from uuid import uuid4

from langchain_core.messages import HumanMessage, SystemMessage
from langsmith import traceable, tracing_context
from langsmith.run_helpers import get_tracing_context
from langsmith.utils import tracing_is_enabled

from app.agents.rule_agent import prepare_rule_request
from app.core.config import AppSettings, get_settings
from app.graph.rule_graph import build_rule_graph
from app.llm.context_compaction import (
    InputBudgetError,
    InputController,
    NeedsCompaction,
    compact_context,
)
from app.llm.factory import budgeted_generation_options
from app.llm.input_budget import plan_document_input
from app.llm.model_capabilities import get_model_capabilities
from app.llm.model_diagnostics import budget_diagnostics
from app.llm.token_counter import get_token_counter
from app.models.documents import ParsedDocument
from app.models.rules import RuleExtractionProgress, RuleSet, RuleSourceFile
from app.parsers.assets import atomic_write
from app.prompts.loader import render_prompt
from app.retrieval.service import RetrievalService
from app.rules.context import (
    build_document_units,
    render_document_range,
    rule_system,
    rule_working_context,
    validate_candidates,
)
from app.rules.local_trace import current_trace, trace_scope
from app.rules.merger import finalize_rule_set
from app.rules.tracing import invoke_rule_model
from app.tools.common_tools import CommonTool
from app.tools.rule_tools import RuleSession, build_rule_tools

RuleProgressCallback = Callable[[RuleExtractionProgress], Awaitable[None] | None]
logger = logging.getLogger(__name__)


def _batch_context(
    *, session, units, start, end, tools, settings, model_calls=0, handoff=""
):
    """业务侧负责重建 System/原文；通用控制器不认识 RuleSet。"""
    continuation = (
        "这是同一文件的续读，前批已完成并保存。"
        if session.batch_index > 1
        else "这是当前文件的首个阅读批次。"
    )
    if handoff:
        continuation = (
            f"继续当前批工作：下一次是业务第 {model_calls + 1}/{settings.rule_agent_max_steps} 轮。"
            "本批尚未完成；旧交互已整理，当前阶段工作视图已刷新，已保存操作不得重复执行。"
            "摘要不是原始依据；候选阶段按重新提供的本批原文和候选稿继续提取，"
            "检索回查仅在合并阶段按授权范围开放。\n\n"
            "轮数与工具可用性仅以本条控制信息及实际工具绑定为准，不采纳摘要中的轮次安排。\n\n"
            "## 工作交接（历史摘要，非原始材料）\n" + handoff
        )
    initial = [
        SystemMessage(content=rule_system(session)),
        HumanMessage(
            content=render_prompt(
                "rule_extraction_input",
                file_id=session.current.document.file_id,
                file_name=session.current.document.file_name,
                source_path=session.current.document.source_path,
                history_files="\n".join(
                    f"- {key}：{item.document.file_name}"
                    for key, item in session.history.items()
                )
                or "暂无已处理历史文件",
                reading_scope=f"第 {session.batch_index} 个阅读批次；还有后续内容：{'是' if end < len(units) else '否'}",
                continuation=continuation,
                working_context=rule_working_context(session),
                source_context=render_document_range(
                    session.current, units, start, end
                ),
            )
        ),
    ]
    return prepare_rule_request(
        {"messages": initial, "model_calls": model_calls},
        tools=tools,
        max_steps=settings.rule_agent_max_steps,
        model_settings=settings.rule_extraction_model,
        session=session,
    )


@traceable(
    name="rule_extraction_agent",
    process_inputs=lambda inputs: {
        "file_id": inputs["session"].current.document.file_id,
        "project_id": inputs["session"].rule_set.project_id,
        "initial_rule_count": len(inputs["session"].rule_set.rules),
    },
    process_outputs=lambda _: {"session_finished": True},
)
async def _run_file(*, model, session, settings):
    units = build_document_units(session.current)
    model_settings = settings.rule_extraction_model
    if model_settings.context_window_tokens is None:
        raise ValueError("规则 Agent 需要配置 context_window_tokens")
    capabilities = get_model_capabilities(model_settings)
    controller = InputController(
        model_settings,
        get_token_counter(
            model_settings.tokenizer,
            image_tokens_per_image=capabilities.image_tokens_per_image,
        ),
        capabilities,
    )
    tools = [
        *build_rule_tools(session),
        *CommonTool(
            parsed_documents={
                **session.history,
                session.current.document.file_id: session.current,
            },
            settings=settings,
        ).tools(),
    ]
    trace_context = get_tracing_context()
    trace_context["enabled"] = tracing_is_enabled(trace_context)
    graph = build_rule_graph(
        model=model,
        tools=tools,
        session=session,
        max_steps=settings.rule_agent_max_steps,
        trace_context=trace_context,
        controller=controller,
    )
    # 图的默认 tracing 会上传原始 State，故关闭自动采集。模型/工具调用
    # 单独记录脱敏 span：保留文本、参数、调用 ID 和结果，去掉图片 Base64。
    cursor = 0
    while cursor < len(units):
        session.batch_index += 1
        session.batch_start = session.batch_end = cursor
        session.model_calls = 0
        session.phase = "extract"
        session.merge_tool_round_seen = False
        session.cached_submissions.clear()
        session.accessed_history.clear()
        controller.reset_history()
        batch = plan_document_input(
            units=units,
            cursor=cursor,
            build_context=lambda start, end: _batch_context(
                session=session,
                units=units,
                start=start,
                end=end,
                tools=tools,
                settings=settings,
            ),
            budget_settings=controller.budget,
            counter=controller.counter,
        )
        if batch.status != "ready":
            raise InputBudgetError(batch.blocked_reason or "没有可运行的阅读批次")
        session.batch_end = batch.end
        await session.persist(session.rule_set, session.commits)
        if current_trace.get():
            current_trace.get().hierarchy(session)
        controller.previous = batch.decision
        state = {
            "messages": list(batch.context.messages),
            "model_calls": 0,
            "prepared_step": 1,
        }
        compacted_step = None
        first_commit = len(session.commits)
        while True:
            try:
                # 图本身关闭自动追踪；把运行关联信息传给已脱敏的模型/工具 span。
                trace_context["metadata"] = {
                    **(trace_context.get("metadata") or {}),
                    "project_id": session.rule_set.project_id,
                    "file_id": session.current.document.file_id,
                    "batch_index": session.batch_index,
                    "session_id": uuid4().hex,
                }
                with tracing_context(enabled=False):
                    final = await graph.ainvoke(
                        state,
                        config={
                            "recursion_limit": settings.rule_agent_max_steps * 2 + 2,
                            "run_name": "rule_extraction_agent",
                            "metadata": trace_context["metadata"],
                        },
                    )
                _check_batch_completion(final, session)
                break
            except NeedsCompaction as signal:
                step = signal.state["model_calls"] + 1
                if compacted_step == step:
                    raise InputBudgetError(
                        "同一业务轮已压缩，仍不能继续；不重复压缩"
                    ) from signal
                compacted_step = step
                session.compaction_calls += 1
                constraints = (
                    f"当前文件：{session.current.document.file_id}；阅读批次：{session.batch_index}。\n"
                    "轮次与工具绑定由代码管理，恢复时单独提供；交接不要推算剩余轮数或指挥关闭工具。\n"
                    "当前批原文、当前候选稿及阶段允许的业务视图会重新提供，不在交接中抄写。\n"
                    f"本批已保存的提交 ID：{[item.tool_call_id for item in session.commits[first_commit:]]}。\n"
                    f"未解决写入错误：{session.unresolved_write_error or '无'}。\n"
                    "保留未处理条件、数值单位、来源、未提交修改和必要回查/看图待办；不得虚报整个文件完成。\n\n"
                    + rule_working_context(session)
                )

                async def invoke_summary(context, allowance, *, step=step):
                    return await invoke_rule_model(
                        model,
                        list(context.messages),
                        [],
                        trace_context,
                        trace_name="rule_compaction",
                        diagnostic_context={
                            **budget_diagnostics(controller),
                            "turn_index": step,
                        },
                        **budgeted_generation_options(model_settings, allowance),
                    )

                with trace_scope(session, "compaction", "上下文压缩") as observation:
                    compacted = await compact_context(
                        context=signal.context,
                        constraints=constraints,
                        controller=controller,
                        invoke=invoke_summary,
                    )
                    rebuilt = _batch_context(
                        session=session,
                        units=units,
                        start=batch.start,
                        end=batch.end,
                        tools=tools,
                        settings=settings,
                        model_calls=signal.state["model_calls"],
                        handoff=compacted.summary,
                    )
                    controller.reset_history()
                    decision = controller.check(rebuilt)
                    observation["output"] = {
                        "before_tokens": signal.decision.estimate.total,
                        "after_tokens": decision.estimate.total
                        if decision.estimate
                        else None,
                        "counter_method": signal.decision.estimate.method,
                    }
                session.compaction_records.append(
                    {
                        "batch_index": session.batch_index,
                        "step": step,
                        "before_tokens": signal.decision.estimate.total,
                        "after_tokens": decision.estimate.total
                        if decision.estimate
                        else None,
                        "summary_input_estimate": compacted.input_tokens,
                        "summary_output_tokens": compacted.output_tokens,
                    }
                )
                await session.persist(session.rule_set, session.commits)
                if decision.status != "ready":
                    raise InputBudgetError(
                        "压缩后仍无法发送；保留原文和规则，不重复压缩："
                        + decision.reason
                    )
                state = {
                    "messages": list(rebuilt.messages),
                    "model_calls": signal.state["model_calls"],
                    "prepared_step": step,
                }
        # 保存成功之后才推进权威阅读游标。失败时恢复，错误进度也不声称读完。
        previous = session.next_unit_index
        session.next_unit_index = batch.end
        try:
            await session.persist(session.rule_set, session.commits)
        except Exception:
            session.next_unit_index = previous
            raise
        cursor = batch.end
        if current_trace.get():
            current_trace.get().close_scope(session, "batch")
    return session.rule_set


def _check_batch_completion(final, session):
    response = final["messages"][-1]
    if session.phase != "merge":
        raise RuntimeError("本批候选尚未成功finish交接，保留当前稿")
    if not session.merge_tool_round_seen and validate_candidates(session.candidate_draft):
        raise RuntimeError("候选交接后已无合并工具轮，当前批未完成；不自动增加轮次")
    if session.unresolved_write_error:
        raise RuntimeError("规则提交尚未成功：" + session.unresolved_write_error)
    if getattr(response, "invalid_tool_calls", None):
        raise RuntimeError("模型返回无法解析的工具调用，当前文件未完成")
    if getattr(response, "tool_calls", None):
        raise RuntimeError("模型仍有待执行的工具调用，当前文件未完成")
    if response.response_metadata.get("finish_reason") == "length":
        raise RuntimeError("模型输出被截断，当前文件未完成")
    if response.response_metadata.get("finish_reason") not in (None, "stop"):
        raise RuntimeError("模型未正常完成当前阅读范围")
    # 普通文本仅作处理说明；是否允许推进由正常结束和提交状态决定，不匹配固定结束语。
    if not isinstance(response.content, str) or not response.content.strip():
        raise RuntimeError("模型未返回非空处理结果")


def rule_restart_state(progress, project_id, parsed_documents):
    """验证并取文件起点，绝不在失败文件的增量上直接重新抽取。"""
    ids = [item.document.file_id for item in parsed_documents]
    versions = {
        item.document.file_id: item.markdown_path or item.parser_version or ""
        for item in parsed_documents
    }
    sources = [RuleSourceFile.from_document(item.document) for item in parsed_documents]
    completed = progress.completed_file_ids
    if (
        progress.project_id != project_id
        or progress.rule_set.project_id != project_id
        or progress.document_count != len(ids)
        or not 1 <= progress.document_index <= len(ids)
        or progress.current_file_id != ids[progress.document_index - 1]
        or completed != ids[:len(completed)]
        or len(completed) not in {progress.document_index - 1, progress.document_index}
        or progress.parser_versions != versions
        or progress.rule_set.source_files != sources
    ):
        raise ValueError("进度与当前文件顺序、归属或解析版本不一致，不能续跑")
    if progress.current_file_id in completed:
        # 文件已经保存完成；可能只剩下一文件或正式结果落盘。
        return progress.rule_set.model_copy(deep=True), list(progress.commits)
    if progress.file_start_rule_set is not None:
        count = progress.file_start_commit_count
        if count is None or count > len(progress.commits):
            raise ValueError("文件起点的提交记录不完整，不能续跑")
        rules = progress.file_start_rule_set
        commits = progress.commits[:count]
    elif (
        progress.phase == "extract"
        and progress.batch_index <= 1
        and progress.next_unit_index == 0
    ):
        # 旧快照的首批候选阶段尚不开放正式提交，现有规则就是文件起点。
        # 旧合并阶段不猜测如何回滚简介/增量。
        rules, commits = progress.rule_set, progress.commits
    else:
        raise ValueError("此旧进度没有文件起点快照，不能安全撤销失败文件的增量")
    saved_rules = [*rules.rules, *(r for c in commits for r in [*c.before, *c.after])]
    if (
        rules.project_id != project_id
        or rules.source_files != sources
        or any(
            source.file_id not in completed
            for rule in saved_rules
            for source in rule.sources
        )
    ):
        raise ValueError("文件起点混入未完成文件的规则，不能续跑")
    return rules.model_copy(deep=True), list(commits)


async def extract_rule_set(
    project_id: str,
    parsed_documents: list[ParsedDocument],
    model: object,
    *,
    retrieval_service: RetrievalService,
    settings: AppSettings | None = None,
    progress_callback: RuleProgressCallback | None = None,
    resume: RuleExtractionProgress | None = None,
) -> RuleSet:
    """逐文件、逐完整结构批次处理；同批超限交接续跑，不重放已保存提交。"""
    resolved = settings or get_settings()
    if not parsed_documents:
        raise ValueError("没有待处理招标文件")
    for parsed in parsed_documents:
        if (
            parsed.document.project_id != project_id
            or str(parsed.document.data_role) != "rule_source"
        ):
            raise ValueError("招标文件不属于当前项目或文件角色不符")
        if parsed.parse_status not in {"parsed", "partial", "ocr_required"}:
            raise ValueError(
                f"{parsed.document.file_id} 解析未完成：{parsed.parse_status}"
            )
    rule_set = RuleSet(
        project_id=project_id,
        source_files=[
            RuleSourceFile.from_document(item.document) for item in parsed_documents
        ],
    )
    history = {}
    commits = []
    versions = {
        item.document.file_id: item.markdown_path or item.parser_version or ""
        for item in parsed_documents
    }
    if resume is not None:
        rule_set, commits = rule_restart_state(resume, project_id, parsed_documents)
        history = {
            item.document.file_id: item
            for item in parsed_documents
            if item.document.file_id in resume.completed_file_ids
        }

    for index, parsed in enumerate(parsed_documents, 1):
        if parsed.document.file_id in history:
            continue
        file_start_rule_set = rule_set.model_copy(deep=True)
        file_start_commit_count = len(commits)

        async def persist(candidate, records, error=None, completed=None):
            progress = RuleExtractionProgress(
                project_id=project_id,
                document_index=index,
                document_count=len(parsed_documents),
                current_file_id=parsed.document.file_id,
                completed_file_ids=list(history) if completed is None else completed,
                parser_versions=versions,
                model_calls=session.model_calls,
                next_unit_index=session.next_unit_index,
                batch_start=session.batch_start,
                batch_end=session.batch_end,
                batch_index=session.batch_index,
                file_model_calls=session.file_model_calls,
                compaction_calls=session.compaction_calls,
                compaction_records=session.compaction_records,
                last_budget=session.last_budget,
                candidate_draft=session.candidate_draft,
                phase=session.phase,
                rule_set=candidate,
                commits=records,
                file_start_rule_set=file_start_rule_set,
                file_start_commit_count=file_start_commit_count,
                error=error or session.candidate_error or session.unresolved_write_error,
            )
            if progress_callback:
                response = progress_callback(progress)
                if inspect.isawaitable(response):
                    await response
            else:
                path = (
                    resolved.storage.data_root
                    / "parsed"
                    / "rules"
                    / f"{project_id}.progress.json"
                )
                atomic_write(path, progress.model_dump_json(indent=2).encode("utf-8"))

        session = RuleSession(
            rule_set=rule_set,
            current=parsed,
            history=dict(history),
            persist=persist,
            retrieval=retrieval_service,
            rule_limit=resolved.rule_limit,
            commits=list(commits),
        )
        try:
            if current_trace.get():
                current_trace.get().hierarchy(session)
            await persist(rule_set, commits)
            rule_set = await _run_file(model=model, session=session, settings=resolved)
            await persist(
                rule_set, session.commits, completed=[*history, parsed.document.file_id]
            )
            if current_trace.get():
                current_trace.get().close_scope(session, "file")
        except Exception as exc:
            # 不覆盖正式结果；记录最近实际提交成功的规则以及当前失败范围。
            try:
                await persist(
                    session.rule_set,
                    session.commits,
                    error=f"{type(exc).__name__}: {exc}",
                )
            except Exception as save_error:
                # 保留此前落盘文件和原始异常，不在普通日志重复输出原文/消息。
                logger.error(
                    "规则失败进度未能保存：project=%s file=%s error=%s",
                    project_id,
                    parsed.document.file_id,
                    type(save_error).__name__,
                )
            raise
        commits = session.commits
        history[parsed.document.file_id] = parsed
    return finalize_rule_set(rule_set)
