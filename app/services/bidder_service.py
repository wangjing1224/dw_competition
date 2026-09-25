"""独立投标方资料：一次整批上传，统一解析/索引，随后串行运行资料入库 Agent。"""

from __future__ import annotations

from typing import BinaryIO

from app.core.config import AppSettings
from app.evidence.ingestion import ingest_evidence_documents
from app.evidence.storage import library_path, progress_path
from app.llm.factory import EvidenceIngestionModels
from app.models.documents import document_ref_from_record
from app.parsers.service import load_parsed_document, prepare_document
from app.retrieval.index import build_document_index
from app.retrieval.models import IndexScope
from app.retrieval.service import RetrievalService
from app.storage.file_store import FileOwner, FileStore
from app.storage.repositories import BidderRecord, BidderRepository, FileRepository


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

    async def resume_bidder(self, bidder_id: str) -> BidderRecord:
        """恢复本次已上传文件，不接受新上传、不重跑已完成文件；管理接口暂不开放。"""
        bidder = self.bidder_repository.require(bidder_id)
        if bidder.status != "evidence_failed":
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
        try:
            self.bidder_repository.update_status(bidder_id, "processing_evidence")
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
                await self._build_index(bidder_id, documents)
            return await self._ingest(
                bidder_id,
                parsed,
                resume=has_progress or library_path(bidder_id, self.settings).exists(),
            )
        except Exception as exc:
            self._failed(bidder_id, exc)
            raise

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
