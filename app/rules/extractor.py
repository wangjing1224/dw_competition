"""逐文件启动规则 Agent；每次提交原子落盘，后续文件只共享规则库。"""

import inspect
import logging
from collections.abc import Awaitable, Callable
from uuid import uuid4

from langchain_core.messages import HumanMessage, SystemMessage
from langsmith import traceable, tracing_context
from langsmith.run_helpers import get_tracing_context
from langsmith.utils import tracing_is_enabled

from app.core.config import AppSettings, get_settings
from app.graph.rule_graph import build_rule_graph
from app.models.documents import ParsedDocument
from app.models.rules import RuleExtractionProgress, RuleSet, RuleSourceFile
from app.parsers.assets import atomic_write
from app.prompts.loader import render_prompt
from app.retrieval.service import RetrievalService
from app.rules.context import build_document_context, build_rule_view
from app.rules.merger import finalize_rule_set
from app.tools.common_tools import CommonTool
from app.tools.rule_tools import RuleSession, build_rule_tools

RuleProgressCallback = Callable[[RuleExtractionProgress], Awaitable[None] | None]
logger = logging.getLogger(__name__)


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
    initial = [
        SystemMessage(
            content=render_prompt(
                "rule_extraction", rule_view=build_rule_view(session.rule_set)
            )
        ),
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
                source_context=build_document_context(session.current),
            )
        ),
    ]
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
    )
    # 图的默认 tracing 会上传原始 State，故关闭自动采集。模型/工具调用
    # 单独记录脱敏 span：保留文本、参数、调用 ID 和结果，去掉图片 Base64。
    with tracing_context(enabled=False):
        final = await graph.ainvoke(
            {"messages": initial, "model_calls": 0},
            config={
                "recursion_limit": settings.rule_agent_max_steps * 2 + 2,
                "run_name": "rule_extraction_agent",
                "metadata": {
                    "project_id": session.rule_set.project_id,
                    "file_id": session.current.document.file_id,
                    "session_id": uuid4().hex,
                },
            },
        )
    response = final["messages"][-1]
    if session.unresolved_write_error:
        raise RuntimeError("规则提交尚未成功：" + session.unresolved_write_error)
    if getattr(response, "invalid_tool_calls", None):
        raise RuntimeError("模型返回无法解析的工具调用，当前文件未完成")
    if response.response_metadata.get("finish_reason") == "length":
        raise RuntimeError("模型输出被截断，当前文件未完成")
    # 不新造收尾 JSON；仅接受约定的普通文本完成信号，不把任意无工具回复当成功。
    if (
        not isinstance(response.content, str)
        or response.content.strip().rstrip("。.!！") != "处理完毕"
    ):
        raise RuntimeError(f"当前文件未完成：{response.content}")
    return session.rule_set


async def extract_rule_set(
    project_id: str,
    parsed_documents: list[ParsedDocument],
    model: object,
    *,
    retrieval_service: RetrievalService,
    settings: AppSettings | None = None,
    progress_callback: RuleProgressCallback | None = None,
) -> RuleSet:
    """完整文件输入，不做固定字符切分或自动压缩；真实超限由调用失败如实暴露。"""
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

    for index, parsed in enumerate(parsed_documents, 1):

        async def persist(candidate, records, error=None, completed=None):
            progress = RuleExtractionProgress(
                project_id=project_id,
                document_index=index,
                document_count=len(parsed_documents),
                current_file_id=parsed.document.file_id,
                completed_file_ids=list(history) if completed is None else completed,
                parser_versions=versions,
                model_calls=session.model_calls,
                rule_set=candidate,
                commits=records,
                error=error,
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
            await persist(rule_set, commits)
            rule_set = await _run_file(model=model, session=session, settings=resolved)
            await persist(
                rule_set, session.commits, completed=[*history, parsed.document.file_id]
            )
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
