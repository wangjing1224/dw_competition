"""审核任务创建、上下文组装和后台生命周期。"""

from __future__ import annotations

from typing import BinaryIO, Protocol

from pydantic import Field

from app.core.config import AppSettings
from app.core.errors import BusinessError
from app.llm.embedding_model import EmbeddingModel
from app.models.common import StrictModel
from app.models.documents import DocumentRef, document_ref_from_record
from app.models.results import AuditResult
from app.models.rules import RuleSet
from app.parsers.service import parse_document
from app.retrieval.index import build_document_index
from app.retrieval.models import IndexScope
from app.services.background_runner import BackgroundRunner
from app.storage.file_store import FileOwner, FileStore
from app.storage.repositories import (
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
        task_repository: AuditTaskRepository | None = None,
        file_repository: FileRepository | None = None,
        file_store: FileStore | None = None,
    ) -> None:
        self.settings = settings
        self.embedding_model = embedding_model
        self.audit_executor = audit_executor
        self.output_finalizer = output_finalizer
        self.background_runner = background_runner
        self.task_repository = task_repository or AuditTaskRepository()
        self.file_repository = file_repository or FileRepository()
        self.file_store = file_store or FileStore(self.file_repository)

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
            records = [self.file_repository.require(file_id) for file_id in file_ids]
            documents = [document_ref_from_record(record) for record in records]
            [parse_document(document) for document in documents]

            task = self.task_repository.require(task_id)
            self.task_repository.update_progress(
                task_id, {"stage": "building_bid_index"}
            )
            await build_document_index(
                IndexScope(
                    scope_id=f"bid-{task_id}",
                    data_role="bid_document",
                    project_id=task.project_id,
                    bidder_id=task.bidder_id,
                    task_id=task_id,
                ),
                documents,
                self.embedding_model,
                self.settings,
            )

            self.task_repository.update_progress(task_id, {"stage": "auditing"})
            result = await self.audit_executor.run(self.build_context(task_id))

            self.task_repository.update_progress(
                task_id, {"stage": "writing_outputs"}
            )
            await self.output_finalizer.finalize(task_id, result)
        except Exception as exc:
            current = self.task_repository.require(task_id)
            if current.status == "running":
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
        return AuditTaskContext(
            task_id=task.task_id,
            project_id=task.project_id,
            bidder_id=task.bidder_id,
            rule_set=rule_set,
            bid_files=[document_ref_from_record(record) for record in records],
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
