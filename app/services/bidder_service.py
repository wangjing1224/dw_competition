"""独立投标方资料：一次整批上传，统一解析/索引，随后串行运行资料入库 Agent。"""

from __future__ import annotations

from typing import BinaryIO

from app.core.config import AppSettings
from app.evidence.ingestion import ingest_evidence_documents
from app.evidence.local_trace import EvidenceTrace, current_trace
from app.evidence.storage import library_path, progress_path
from app.llm.factory import EvidenceIngestionModels
from app.models.documents import document_ref_from_record
from app.parsers.service import load_parsed_document, prepare_document
from app.retrieval.index import build_document_index
from app.retrieval.models import IndexScope
from app.retrieval.service import RetrievalService
from app.storage.bidder_management import management, update_management
from app.storage.file_store import FileOwner, FileStore
from app.storage.repositories import (
    BidderRecord,
    BidderRepository,
    FileRepository,
    utc_now,
)


class BidderService:
    def __init__(
        self,
        settings: AppSettings,
        models: EvidenceIngestionModels,
        *,
        bidder_repository: BidderRepository | None = None,
        file_repository: FileRepository | None = None,
        file_store: FileStore | None = None,
    ) -> None:
        self.settings, self.models = settings, models
        self.bidder_repository = bidder_repository or BidderRepository()
        self.file_repository = file_repository or FileRepository()
        self.file_store = file_store or FileStore(self.file_repository)

    async def create_bidder(
        self, uploads: list[tuple[str, str, BinaryIO]]
    ) -> BidderRecord:
        if not uploads:
            raise ValueError("投标方资料不能为空")
        bidder = self.bidder_repository.create(status="uploading_evidence")
        uploaded = False
        try:
            documents = [
                document_ref_from_record(
                    self.file_store.save(
                        owner=FileOwner(bidder_id=bidder.bidder_id),
                        data_role="evidence_material",
                        stream=stream,
                        file_name=file_name,
                        relative_path=relative_path,
                    )
                )
                for file_name, relative_path, stream in uploads
            ]
            uploaded = True
            self.bidder_repository.update_status(
                bidder.bidder_id, "processing_evidence"
            )
            parsed = [await prepare_document(document) for document in documents]
            # 一次构建全批范围；底层是整库原子替换，不是逐文件 append。
            await self._build_index(bidder.bidder_id, documents)
            return await self._ingest(bidder.bidder_id, parsed)
        except Exception as exc:
            self._failed(
                bidder.bidder_id, exc, code=None if uploaded else "upload_incomplete"
            )
            raise

    async def resume_bidder(self, bidder_id: str, *, claimed=False) -> BidderRecord:
        """恢复固定上传批次；HTTP 入口已原子占用状态，不重跑已完成文件。"""
        bidder = self.bidder_repository.require(bidder_id)
        expected = "processing_evidence" if claimed else "evidence_failed"
        if bidder.status != expected:
            raise ValueError("仅允许恢复失败的本批入库，不能重跑已完成资料库")
        if bidder.error_code == "upload_incomplete":
            raise ValueError(
                "上传未完成，不能把已保存的部分文件当成整批恢复；请重新创建独立批次"
            )
        records = self.file_repository.list_by_scope(
            bidder_id=bidder_id, data_role="evidence_material"
        )
        documents = [document_ref_from_record(record) for record in records]
        if not documents:
            raise ValueError("没有可恢复的上传文件")
        has_progress = progress_path(bidder_id, self.settings).exists()
        trace = EvidenceTrace(bidder_id)
        token = current_trace.set(trace)
        failure = None
        phase = "ingesting" if has_progress else "parsing"
        try:
            self.bidder_repository.update_status(bidder_id, "processing_evidence")
            update_management(bidder_id, phase=phase, trace_status="recording")
            parsed = []
            for document in documents:
                try:
                    parsed.append(load_parsed_document(document.file_id))
                except FileNotFoundError:
                    if has_progress:
                        raise ValueError(
                            "恢复所需解析快照丢失，不能重新解析后跳过旧进度"
                        )
                    parsed.append(await prepare_document(document))
            if not has_progress:
                phase = "indexing"
                update_management(bidder_id, phase=phase)
                await self._build_index(bidder_id, documents)
            phase = "ingesting"
            update_management(bidder_id, phase=phase)
            result = await self._ingest(
                bidder_id,
                parsed,
                resume=has_progress or library_path(bidder_id, self.settings).exists(),
            )
            update_management(bidder_id, phase="completed", current_file_id=None)
            return result
        except BaseException as exc:
            failure = exc
            self._failed(bidder_id, exc)
            update_management(bidder_id, phase="failed", failed_phase=phase)
            raise
        finally:
            trace.close(failure)
            current_trace.reset(token)

    async def process_bidder(self, bidder_id: str) -> BidderRecord:
        """新管理接口已固定上传清单；从登记 ID 重开文件执行，不再读取请求流。"""
        trace = EvidenceTrace(bidder_id)
        token = current_trace.set(trace)
        try:
            bidder = self.bidder_repository.require(bidder_id)
            if bidder.status != "processing_evidence":
                raise ValueError("仅能处理已受理的新资料库")
            update_management(bidder_id, phase="parsing", started_at=utc_now())
            documents = [
                document_ref_from_record(record)
                for record in self.file_repository.list_by_scope(
                    bidder_id=bidder_id, data_role="evidence_material"
                )
            ]
            if not documents:
                raise ValueError("资料文件清单为空")
            parsed = []
            for document in documents:
                update_management(bidder_id, current_file_id=document.file_id)
                parsed.append(await prepare_document(document))
            update_management(bidder_id, phase="indexing", current_file_id=None)
            await self._build_index(bidder_id, documents)
            update_management(bidder_id, phase="ingesting")
            result = await self._ingest(bidder_id, parsed)
            update_management(bidder_id, phase="completed", current_file_id=None)
            trace.close()
            return result
        except BaseException as exc:
            self._failed(bidder_id, exc)
            update_management(
                bidder_id,
                phase="failed",
                failed_phase=management(bidder_id).get("phase", "received"),
            )
            trace.close(exc)
            raise
        finally:
            current_trace.reset(token)

    async def _build_index(self, bidder_id, documents):
        await build_document_index(
            IndexScope(
                scope_id=f"evidence-{bidder_id}",
                data_role="evidence_material",
                bidder_id=bidder_id,
            ),
            documents,
            self.models.embedding,
            self.settings,
        )

    async def _ingest(self, bidder_id, parsed, *, resume=False):
        await ingest_evidence_documents(
            bidder_id,
            parsed,
            self.models.evidence_classification,
            retrieval_service=RetrievalService(
                self.models.embedding, self.models.rerank, self.settings
            ),
            settings=self.settings,
            resume=resume,
        )
        return self.bidder_repository.update_status(bidder_id, "evidence_ready")

    def _failed(self, bidder_id, exc, *, code=None):
        self.bidder_repository.update_status(
            bidder_id,
            "evidence_failed",
            error_code=code or type(exc).__name__,
            error_message=str(exc),
        )
