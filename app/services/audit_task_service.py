"""审核任务创建、上下文组装和后台生命周期。"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC
from typing import BinaryIO, Literal, Protocol

from pydantic import Field

from app.audit.bid_summary import (
    summarize_and_route_bid_document,
    write_bid_summary,
)
from app.audit.concurrency import model_limiter
from app.audit.context import fixed_parsed, verify_indexes
from app.core.config import AppSettings, ChatModelSettings
from app.core.errors import BusinessError
from app.evidence.storage import load_library
from app.llm.embedding_model import EmbeddingModel
from app.models.common import StrictModel
from app.models.documents import (
    BidDocumentSummary,
    DocumentRef,
    ParsedDocument,
    document_ref_from_record,
)
from app.models.evidence import EvidenceLibrary
from app.models.results import AuditResult, FileAuditResult, ProjectMatch, ReportContext
from app.models.rules import RuleSet
from app.parsers.assets import atomic_write
from app.parsers.service import prepare_document
from app.prompts.loader import load_prompt
from app.retrieval.index import build_document_index
from app.retrieval.models import IndexScope
from app.services.background_runner import BackgroundRunner
from app.storage.file_store import FileOwner, FileStore
from app.storage.repositories import (
    AuditEventRepository,
    AuditTaskRecord,
    AuditTaskRepository,
    BidderRepository,
    FileRepository,
    ProjectRepository,
)

logger = logging.getLogger(__name__)


class AuditTaskContext(StrictModel):
    task_id: str
    project_id: str
    bidder_id: str
    rule_set: RuleSet
    bid_files: list[DocumentRef] = Field(default_factory=list)
    bid_summaries: dict[str, BidDocumentSummary] = Field(default_factory=dict)
    routed_file_results: list[FileAuditResult] = Field(default_factory=list)
    evidence_library: EvidenceLibrary
    parsed_documents: dict[str, ParsedDocument] = Field(default_factory=dict)
    file_order: list[str] = Field(default_factory=list)
    file_errors: dict[str, str] = Field(default_factory=dict)
    report_template: str
    report_format_version: Literal[1, 2, 3] = 1
    report_context: ReportContext | None = None
    index_scope_overrides: dict[str, str] = Field(default_factory=dict)


class AuditExecutor(Protocol):
    async def run(self, context: AuditTaskContext) -> AuditResult: ...


class OutputFinalizer(Protocol):
    async def finalize(self, task_id: str, result: AuditResult) -> None: ...


class AuditTaskService:
    def __init__(
        self,
        settings: AppSettings,
        embedding_model: EmbeddingModel,
        audit_executor: AuditExecutor,
        output_finalizer: OutputFinalizer,
        background_runner: BackgroundRunner,
        *,
        summary_model: object | None = None,
        summary_model_settings: ChatModelSettings | None = None,
        task_repository: AuditTaskRepository | None = None,
        file_repository: FileRepository | None = None,
        file_store: FileStore | None = None,
        event_repository: AuditEventRepository | None = None,
    ) -> None:
        self.settings = settings
        self.embedding_model = embedding_model
        self.summary_model = summary_model
        self.summary_model_settings = (
            summary_model_settings or settings.bid_summary_model
        )
        self.audit_executor = audit_executor
        self.output_finalizer = output_finalizer
        self.background_runner = background_runner
        self.task_repository = task_repository or AuditTaskRepository()
        self.file_repository = file_repository or FileRepository()
        self.file_store = file_store or FileStore(self.file_repository)
        self.event_repository = event_repository or AuditEventRepository()

    async def create_task(
        self,
        project_id: str,
        bidder_id: str,
        uploads: list[tuple[str, str, BinaryIO]],
    ) -> AuditTaskRecord:
        if not uploads:
            raise BusinessError("empty_audit_files", "审核测试集不能为空")
        project = ProjectRepository().require(project_id)
        if project.status != "rule_ready":
            raise BusinessError(
                "rule_not_ready", "项目规则尚未准备完成", http_status=409
            )
        bidder = BidderRepository().require(bidder_id)
        if bidder.status != "evidence_ready":
            raise BusinessError(
                "evidence_not_ready",
                "投标方资料尚未准备完成",
                http_status=409,
            )
        rule_set, library, parsed = self._load_libraries(project, bidder)
        task = self.task_repository.create(
            project_id,
            bidder_id,
            progress={
                "stage": "saving_files",
                "completed": 0,
                "total": len(uploads),
            },
        )
        self.event_repository.append(
            task.task_id,
            actor="pipeline",
            event_type="task_created",
            title="审核任务已创建",
            detail={"file_count": len(uploads)},
        )
        try:
            records = [
                self.file_store.save(
                    owner=FileOwner(project_id, bidder_id, task.task_id),
                    data_role="bid_document",
                    stream=stream,
                    file_name=file_name,
                    relative_path=relative_path,
                )
                for file_name, relative_path, stream in uploads
            ]
        except Exception as exc:
            self.task_repository.mark_failed(
                task.task_id,
                error_code=type(exc).__name__,
                error_message=str(exc),
            )
            raise
        try:
            context = AuditTaskContext(
                task_id=task.task_id,
                project_id=project_id,
                bidder_id=bidder_id,
                rule_set=rule_set,
                evidence_library=library,
                parsed_documents=parsed,
                file_order=[record.file_id for record in records],
                report_template=load_prompt("audit_report_template"),
                report_format_version=3,
                report_context=ReportContext(),
            )
            self._save_context(context)
        except Exception as exc:
            self.task_repository.mark_failed(
                task.task_id, error_code=type(exc).__name__, error_message=str(exc)
            )
            raise
        self.background_runner.submit(self._run_task, task.task_id, context.file_order)
        return self.task_repository.require(task.task_id)

    async def _run_task(self, task_id: str, file_ids: list[str]) -> None:
        """后台阶段失败统一写入 failed；报告模块自行处理 Word 部分失败。"""

        try:
            self.task_repository.update_progress(
                task_id, {"stage": "parsing_bid_documents"}
            )
            self.event_repository.append(
                task_id,
                actor="pipeline",
                event_type="stage_changed",
                title="开始解析投标文件",
                detail={"file_count": len(file_ids)},
            )
            context = self.build_context(task_id)
            if self.summary_model is None:
                raise RuntimeError("未配置路由模型，不能默认放行")
            parsed_documents = []
            for file_id in file_ids:
                document = document_ref_from_record(
                    self.file_repository.require(file_id)
                )
                try:
                    parsed_documents.append(await prepare_document(document))
                except Exception as exc:
                    logger.exception("投标文件解析失败 file_id=%s", file_id)
                    context.file_errors[file_id] = (
                        f"解析失败：{type(exc).__name__}: {exc}"
                    )

            self.task_repository.update_progress(
                task_id, {"stage": "routing_bid_documents"}
            )
            limit = asyncio.Semaphore(self.settings.audit.file_concurrency)

            async def route(parsed):
                async with limit:
                    return await summarize_and_route_bid_document(
                        parsed,
                        context.rule_set,
                        self.summary_model,
                        self.summary_model_settings,
                        limiter=model_limiter(self.settings.audit.model_concurrency),
                        trace_context={
                            "metadata": {
                                "task_id": task_id,
                                "file_id": parsed.document.file_id,
                            }
                        },
                    )

            outcomes = await asyncio.gather(
                *(route(parsed) for parsed in parsed_documents), return_exceptions=True
            )
            for parsed, value in zip(parsed_documents, outcomes, strict=True):
                file_id = parsed.document.file_id
                if isinstance(value, asyncio.CancelledError):
                    raise value
                if isinstance(value, Exception):
                    logger.error(
                        "投标文件路由失败 file_id=%s",
                        file_id,
                        exc_info=(type(value), value, value.__traceback__),
                    )
                    context.file_errors[file_id] = (
                        f"路由失败：{type(value).__name__}: {value}"
                    )
                    continue
                context.bid_summaries[file_id] = value
                write_bid_summary(value, self.settings)
                if value.match_status == "matched":
                    context.bid_files.append(parsed.document)
                    context.parsed_documents[file_id] = parsed
                else:
                    context.routed_file_results.append(
                        _routed_file_result(
                            parsed.document,
                            value,
                            version=context.report_format_version,
                            report_context=context.report_context,
                        )
                    )
                self.event_repository.append(
                    task_id,
                    actor="pipeline",
                    event_type="bid_document_routed",
                    title="投标文件相关性判断完成",
                    detail={
                        "file_id": file_id,
                        "match_status": value.match_status,
                        "reason": value.match_reason,
                    },
                )
            self._save_context(context)
            if context.bid_files:
                self.task_repository.update_progress(
                    task_id, {"stage": "building_bid_index"}
                )
                await build_document_index(
                    IndexScope(
                        scope_id=f"bid-{task_id}",
                        data_role="bid_document",
                        project_id=context.project_id,
                        bidder_id=context.bidder_id,
                        task_id=task_id,
                    ),
                    context.bid_files,
                    self.embedding_model,
                    self.settings,
                )
            self.task_repository.update_progress(task_id, {"stage": "auditing"})
            result = await self.audit_executor.run(context)

            self.task_repository.update_progress(task_id, {"stage": "writing_outputs"})
            self.event_repository.append(
                task_id,
                actor="pipeline",
                event_type="stage_changed",
                title="智能审核完成，开始生成报告",
                detail={"file_result_count": len(result.file_results)},
            )
            await self.output_finalizer.finalize(task_id, result)
            self.event_repository.append(
                task_id,
                actor="pipeline",
                event_type="task_failed" if result.file_errors else "task_completed",
                title="审核任务处理结束",
                detail={"file_result_count": len(result.file_results)},
            )
        except Exception as exc:
            current = self.task_repository.require(task_id)
            if current.status == "running":
                self.event_repository.append(
                    task_id,
                    actor="pipeline",
                    event_type="task_failed",
                    title="审核任务执行失败",
                    detail={
                        "error_code": getattr(
                            exc,
                            "code",
                            type(exc).__name__,
                        ),
                        "error_message": str(exc),
                    },
                )
                self.task_repository.mark_failed(
                    task_id,
                    error_code=getattr(exc, "code", type(exc).__name__),
                    error_message=str(exc),
                )
            raise

    def _load_libraries(self, project, bidder):
        if not project.rule_set_path:
            raise BusinessError("rule_set_missing", "项目规则库路径缺失")
        rules = RuleSet.model_validate_json(
            self.file_store.resolve_relative(project.rule_set_path).read_text("utf-8")
        )
        library = load_library(bidder.bidder_id, self.settings)
        if rules.project_id != project.project_id or not rules.rules:
            raise ValueError("规则库归属错误或规则为空")
        if library is None or library.bidder_id != bidder.bidder_id:
            raise ValueError("资料库缺失或归属错误")
        if not library.materials or {x.file_id for x in library.materials} != {
            x.file_id for x in library.source_files
        }:
            raise ValueError("资料库尚未完成所有登记文件的入库")
        parsed = fixed_parsed(
            rules.source_files,
            self.file_repository,
            role="rule_source",
            project_id=project.project_id,
        )
        parsed.update(
            fixed_parsed(
                library.source_files,
                self.file_repository,
                role="evidence_material",
                bidder_id=bidder.bidder_id,
            )
        )
        verify_indexes(parsed, self.settings)
        return rules, library, parsed

    def _save_context(self, context):
        path = self.file_store.task_output_dir(context.task_id) / "audit_context.json"
        atomic_write(path, context.model_dump_json(indent=2).encode("utf-8"))

    def build_context(self, task_id):
        path = self.file_store.task_output_dir(task_id) / "audit_context.json"
        return AuditTaskContext.model_validate_json(path.read_text("utf-8"))

    def get_task(self, task_id: str) -> dict[str, object]:
        return query_audit_task(task_id, self.task_repository, self.file_store)


def query_audit_task(
    task_id: str,
    task_repository: AuditTaskRepository | None = None,
    file_store: FileStore | None = None,
) -> dict[str, object]:
    """纯存储查询，不构造 Chat/Embedding/Rerank 客户端。"""

    repository = task_repository or AuditTaskRepository()
    store = file_store or FileStore()
    task = repository.require(task_id)
    response: dict[str, object] = {
        "task_id": task.task_id,
        "project_id": task.project_id,
        "bidder_id": task.bidder_id,
        "status": task.status,
        "progress": task.progress,
        "events": [
            {
                "event_id": event.event_id,
                "actor": event.actor,
                "event_type": event.event_type,
                "title": event.title,
                "detail": event.detail,
                "created_at": event.created_at,
            }
            for event in AuditEventRepository().list_by_task(task_id)
        ],
    }
    if task.json_path:
        response["outputs"] = {
            "json": task.json_path,
            "word": task.word_paths or [],
        }
        if task.json_path:
            response["result"] = AuditResult.model_validate_json(
                store.resolve_relative(task.json_path).read_text("utf-8")
            ).model_dump(mode="json")
    if task.status in {"failed", "completed_with_report_error"}:
        response["error"] = {
            "code": task.error_code,
            "message": task.error_message,
        }
    return response


def _routed_file_result(
    document: DocumentRef,
    summary: BidDocumentSummary,
    *,
    version=1,
    report_context=None,
) -> FileAuditResult:
    from datetime import datetime

    not_matched = summary.match_status == "not_matched"
    result = FileAuditResult(
        file=document,
        report_format_version=version,
        report_context=report_context.model_copy(
            update={"finalized_at": report_context.finalized_at or datetime.now(UTC)}
        )
        if report_context
        else None,
        is_applicable=False,
        project_match=ProjectMatch(
            status=summary.match_status,
            reason=summary.match_reason,
        ),
        exclusion_reason=summary.match_reason,
        overall_conclusion=("not_applicable" if not_matched else "manual_review"),
        suggestions=(
            []
            if not_matched
            else ["请人工确认该文件是否属于当前招标项目后再决定是否审核"]
        ),
    )
    result.report_title = result.display_title
    return result
