"""审核任务创建、上下文组装和后台生命周期。"""

from __future__ import annotations

from typing import BinaryIO, Protocol

from pydantic import Field

from app.audit.bid_summary import (
    load_bid_summary,
    summarize_and_route_bid_document,
    write_bid_summary,
)
from app.core.config import AppSettings, ChatModelSettings
from app.core.errors import BusinessError
from app.evidence.catalog import build_evidence_catalog
from app.evidence.storage import load_evidence_classification
from app.llm.embedding_model import EmbeddingModel
from app.models.common import StrictModel
from app.models.documents import (
    BidDocumentSummary,
    DocumentRef,
    document_ref_from_record,
)
from app.models.evidence import EvidenceCatalogItem, EvidenceClassificationRecord
from app.models.results import AuditResult, FileAuditResult, ProjectMatch
from app.models.rules import RuleSet
from app.parsers.service import prepare_document
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


class AuditTaskContext(StrictModel):
    task_id: str
    project_id: str
    bidder_id: str
    rule_set: RuleSet
    bid_files: list[DocumentRef] = Field(default_factory=list)
    bid_summaries: dict[str, BidDocumentSummary] = Field(default_factory=dict)
    routed_file_results: list[FileAuditResult] = Field(default_factory=list)
    evidence_catalog: dict[str, list[EvidenceCatalogItem]] = Field(
        default_factory=dict
    )
    evidence_classifications: list[EvidenceClassificationRecord] = Field(
        default_factory=list
    )


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
            summary_model_settings or settings.bid_chat_model
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
            raise BusinessError("rule_not_ready", "项目规则尚未准备完成", http_status=409)
        bidder = BidderRepository().require(project_id, bidder_id)
        if bidder.status != "evidence_ready":
            raise BusinessError(
                "evidence_not_ready",
                "投标方资料尚未准备完成",
                http_status=409,
            )
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
        self.background_runner.submit(
            self._run_task,
            task.task_id,
            [record.file_id for record in records],
        )
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
            records = [self.file_repository.require(file_id) for file_id in file_ids]
            documents = [document_ref_from_record(record) for record in records]
            parsed_documents = [
                await prepare_document(document) for document in documents
            ]

            task = self.task_repository.require(task_id)
            project = ProjectRepository().require(task.project_id)
            if not project.rule_set_path:
                raise BusinessError("rule_set_missing", "项目 RuleSet 路径缺失")
            rule_set = RuleSet.model_validate_json(
                self.file_store.resolve_relative(project.rule_set_path).read_text("utf-8")
            )

            self.task_repository.update_progress(
                task_id, {"stage": "summarizing_bid_documents"}
            )
            self.event_repository.append(
                task_id,
                actor="pipeline",
                event_type="stage_changed",
                title="开始生成投标文件摘要并判断项目相关性",
            )
            matched_documents: list[DocumentRef] = []
            for parsed in parsed_documents:
                if self.summary_model is None:
                    summary = BidDocumentSummary(
                        file_id=parsed.document.file_id,
                        summary="测试执行器未配置摘要模型，默认进入审核",
                        match_status="matched",
                        match_reason="测试执行器默认放行",
                    )
                else:
                    summary = await summarize_and_route_bid_document(
                        parsed,
                        rule_set,
                        self.summary_model,
                        self.summary_model_settings,
                    )
                write_bid_summary(summary, self.settings)
                if summary.match_status == "matched":
                    matched_documents.append(parsed.document)
                self.event_repository.append(
                    task_id,
                    actor="pipeline",
                    event_type="bid_document_routed",
                    title="投标文件相关性判断完成",
                    detail={
                        "file_id": parsed.document.file_id,
                        "file_name": parsed.document.file_name,
                        "match_status": summary.match_status,
                        "reason": summary.match_reason,
                    },
                )

            self.task_repository.update_progress(
                task_id, {"stage": "building_bid_index"}
            )
            self.event_repository.append(
                task_id,
                actor="pipeline",
                event_type="stage_changed",
                title="开始构建投标文件索引",
            )
            if matched_documents:
                await build_document_index(
                    IndexScope(
                        scope_id=f"bid-{task_id}",
                        data_role="bid_document",
                        project_id=task.project_id,
                        bidder_id=task.bidder_id,
                        task_id=task_id,
                    ),
                    matched_documents,
                    self.embedding_model,
                    self.settings,
                )

            self.task_repository.update_progress(task_id, {"stage": "auditing"})
            self.event_repository.append(
                task_id,
                actor="pipeline",
                event_type="stage_changed",
                title="进入智能审核阶段",
            )
            result = await self.audit_executor.run(self.build_context(task_id))

            self.task_repository.update_progress(
                task_id, {"stage": "writing_outputs"}
            )
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
                event_type="task_completed",
                title="审核任务执行完成",
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

    def build_context(self, task_id: str) -> AuditTaskContext:
        task = self.task_repository.require(task_id)
        project = ProjectRepository().require(task.project_id)
        if not project.rule_set_path:
            raise BusinessError("rule_set_missing", "项目 RuleSet 路径缺失")
        rule_set = RuleSet.model_validate_json(
            self.file_store.resolve_relative(project.rule_set_path).read_text("utf-8")
        )
        records = self.file_repository.list_by_scope(
            project_id=task.project_id,
            bidder_id=task.bidder_id,
            task_id=task.task_id,
            data_role="bid_document",
        )
        files = [document_ref_from_record(record) for record in records]
        summaries = {
            file.file_id: load_bid_summary(file.file_id, self.settings)
            for file in files
        }
        routed_results = [
            _routed_file_result(file, summaries[file.file_id])
            for file in files
            if summaries[file.file_id].match_status != "matched"
        ]
        evidence_documents = [
            document_ref_from_record(record)
            for record in self.file_repository.list_by_scope(
                project_id=task.project_id,
                bidder_id=task.bidder_id,
                data_role="evidence_material",
            )
        ]
        evidence_classifications: list[EvidenceClassificationRecord] = []
        for document in evidence_documents:
            classification = load_evidence_classification(
                document.file_id,
                self.settings,
            )
            if classification is not None:
                evidence_classifications.append(
                    EvidenceClassificationRecord(
                        file_id=document.file_id,
                        file_name=document.file_name,
                        **classification.model_dump(),
                    )
                )
        return AuditTaskContext(
            task_id=task.task_id,
            project_id=task.project_id,
            bidder_id=task.bidder_id,
            rule_set=rule_set,
            bid_files=[
                file
                for file in files
                if summaries[file.file_id].match_status == "matched"
            ],
            bid_summaries=summaries,
            routed_file_results=routed_results,
            evidence_catalog={
                str(category): entry
                for category, entry in build_evidence_catalog(
                    evidence_documents,
                    self.settings,
                ).items()
            },
            evidence_classifications=evidence_classifications,
        )

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
    if task.status in {"completed", "completed_with_report_error"}:
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
) -> FileAuditResult:
    not_matched = summary.match_status == "not_matched"
    return FileAuditResult(
        file=document,
        is_applicable=False,
        project_match=ProjectMatch(
            status=summary.match_status,
            reason=summary.match_reason,
        ),
        document_summary=summary,
        exclusion_reason=summary.match_reason,
        overall_conclusion=(
            "not_applicable" if not_matched else "manual_review"
        ),
        suggestions=(
            []
            if not_matched
            else ["请人工确认该文件是否属于当前招标项目后再决定是否审核"]
        ),
    )
