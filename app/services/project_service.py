"""项目创建、招标文件解析、规则抽取和依据索引构建。"""

from __future__ import annotations

from pathlib import Path
from typing import BinaryIO

from app.core.config import AppSettings
from app.llm.factory import ModelRegistry, build_model_registry
from app.models.documents import document_ref_from_record
from app.models.rules import RuleExtractionProgress, RuleSet
from app.parsers.assets import atomic_write
from app.parsers.service import prepare_document
from app.retrieval.index import build_document_index
from app.retrieval.models import IndexScope
from app.retrieval.service import RetrievalService
from app.rules.extractor import extract_rule_set, rule_restart_state
from app.rules.local_trace import LocalTrace, current_trace
from app.services.source_preview import SourcePreviewService
from app.storage.file_store import FileOwner, FileStore
from app.storage.project_management import management, update_management
from app.storage.repositories import (
    FileRepository,
    ProjectRecord,
    ProjectRepository,
    utc_now,
)


class ProjectService:
    """业务预处理流程；HTTP 上传接收完成后只把 project_id 交给后台。"""

    def __init__(
        self,
        settings: AppSettings,
        models: ModelRegistry | None = None,
        *,
        repository: ProjectRepository | None = None,
        file_store: FileStore | None = None,
    ) -> None:
        self.settings = settings
        self.models = models
        self.repository = repository or ProjectRepository()
        self.file_store = file_store or FileStore()

    async def create_project(
        self,
        uploads: list[tuple[str, str, BinaryIO]],
    ) -> ProjectRecord:
        project = self.repository.create()
        try:
            for file_name, relative_path, stream in uploads:
                self.file_store.save(
                    owner=FileOwner(project_id=project.project_id),
                    data_role="rule_source",
                    stream=stream,
                    file_name=file_name,
                    relative_path=relative_path,
                )
        except Exception as exc:
            self.repository.update_status(
                project.project_id,
                "rule_failed",
                error_code=type(exc).__name__,
                error_message=str(exc),
            )
            raise
        return await self.process_project(project.project_id)

    def load_resume(self, project_id: str):
        """只读校验旧进度与原解析快照；不重新解析、不调用模型。"""
        progress = RuleExtractionProgress.model_validate_json(
            self._rule_progress_path(project_id).read_bytes()
        )
        records = FileRepository().list_by_scope(
            project_id=project_id, data_role="rule_source"
        )
        preview = SourcePreviewService(self.settings, self.file_store)
        parsed = []
        for record in records:
            _, document = preview.parsed(record)
            if (
                document.source_sha256 != record.sha256
                or document.document.file_name != record.file_name
                or document.document.source_path != record.source_path
                or document.parse_status not in {"parsed", "partial", "ocr_required"}
            ):
                raise ValueError("原文件与解析快照不一致，不能续跑")
            parsed.append(document)
        rule_restart_state(progress, project_id, parsed)
        return progress, parsed

    async def resume_project(self, project_id: str) -> ProjectRecord:
        return await self.process_project(project_id, resume=True)

    async def process_project(
        self, project_id: str, *, resume: bool = False
    ) -> ProjectRecord:
        project = self.repository.require(project_id)
        if resume and project.status != "processing_rules":
            raise ValueError("规则库未受理重试或已经结束，请刷新状态")
        trace = LocalTrace(project_id)
        token = current_trace.set(trace)
        failure = None
        phase = "extracting" if resume else "received"
        try:
            update_management(
                project_id,
                phase=phase,
                started_at=management(project_id).get("started_at") or utc_now(),
                trace_status="recording",
            )
            # 在后台才构造模型，配置错误也能通过进度接口查看，而非丢失上传结果。
            models = self.models or build_model_registry(self.settings)
            checkpoint = None
            if resume:
                checkpoint, parsed = self.load_resume(project_id)
            else:
                records = FileRepository().list_by_scope(
                    project_id=project_id, data_role="rule_source"
                )
                documents = [document_ref_from_record(record) for record in records]
                parsed = []
                phase = "parsing"
                for document in documents:
                    update_management(
                        project_id, phase=phase, current_file_id=document.file_id
                    )
                    parsed.append(await prepare_document(document))
                if not parsed:
                    raise ValueError("没有待处理招标文件")
                phase = "indexing"
                update_management(project_id, phase=phase, current_file_id=None)
                await build_document_index(
                    IndexScope(
                        scope_id=f"rules-{project.project_id}",
                        data_role="rule_source",
                        project_id=project.project_id,
                    ),
                    documents,
                    models.embedding,
                    self.settings,
                )
            phase = "extracting"
            update_management(project_id, phase=phase)
            extracted = await extract_rule_set(
                project.project_id,
                parsed,
                models.rule_extraction,
                settings=self.settings,
                retrieval_service=RetrievalService(
                    models.embedding, models.rerank, self.settings
                ),
                progress_callback=self._write_rule_progress,
                **({"resume": checkpoint} if resume else {}),
            )
            phase = "saving"
            update_management(project_id, phase=phase, current_file_id=None)
            rule_path = self._write_rule_set(extracted)
            result = self.repository.update_status(
                project.project_id,
                "rule_ready",
                rule_set_path=self.file_store.relative_to_data_root(rule_path),
            )
            update_management(project_id, phase="completed")
            return result
        except BaseException as exc:
            failure = exc
            self.repository.update_status(
                project.project_id,
                "rule_failed",
                error_code=type(exc).__name__,
                error_message=str(exc),
            )
            update_management(project_id, phase="failed", failed_phase=phase)
            raise
        finally:
            trace.close(failure)
            current_trace.reset(token)

    def _write_rule_set(self, rule_set: RuleSet) -> Path:
        """RuleSet 使用临时文件校验后原子替换，避免半写入状态。"""

        directory = self.settings.storage.data_root / "parsed" / "rules"
        path = directory / f"{rule_set.project_id}.json"
        payload = rule_set.model_dump_json(indent=2)
        RuleSet.model_validate_json(payload)
        atomic_write(path, payload.encode("utf-8"))
        return path

    def _rule_progress_path(self, project_id: str) -> Path:
        return (
            self.settings.storage.data_root
            / "parsed"
            / "rules"
            / f"{project_id}.progress.json"
        )

    def _write_rule_progress(self, progress: RuleExtractionProgress) -> None:
        """每次有效提交及文件结束写入快照，失败时保留最近已保存成果。"""

        path = self._rule_progress_path(progress.project_id)
        payload = progress.model_dump_json(indent=2)
        RuleExtractionProgress.model_validate_json(payload)
        atomic_write(path, payload.encode("utf-8"))
