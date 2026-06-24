"""投标方资料保存、解析、分类和 Evidence Index 构建。"""

from __future__ import annotations

from typing import BinaryIO

from app.core.config import AppSettings
from app.evidence.classifier import classify_evidence_document
from app.evidence.storage import write_evidence_classification
from app.llm.factory import ModelRegistry
from app.models.documents import document_ref_from_record
from app.parsers.service import parse_document
from app.retrieval.index import build_document_index
from app.retrieval.models import IndexScope
from app.storage.file_store import FileOwner, FileStore
from app.storage.repositories import (
    BidderRecord,
    BidderRepository,
    FileRepository,
    ProjectRepository,
)


class BidderService:
    def __init__(
        self,
        settings: AppSettings,
        models: ModelRegistry,
        *,
        bidder_repository: BidderRepository | None = None,
        file_repository: FileRepository | None = None,
        file_store: FileStore | None = None,
    ) -> None:
        self.settings = settings
        self.models = models
        self.bidder_repository = bidder_repository or BidderRepository()
        self.file_repository = file_repository or FileRepository()
        self.file_store = file_store or FileStore(self.file_repository)

    async def create_bidder(
        self,
        project_id: str,
        uploads: list[tuple[str, str, BinaryIO]],
    ) -> BidderRecord:
        project = ProjectRepository().require(project_id)
        if project.status != "rule_ready":
            raise ValueError("项目规则尚未准备完成")
        bidder = self.bidder_repository.create(project_id)
        try:
            indexed_documents = []
            for file_name, relative_path, stream in uploads:
                record = self.file_store.save(
                    owner=FileOwner(
                        project_id=project_id, bidder_id=bidder.bidder_id
                    ),
                    data_role="evidence_material",
                    stream=stream,
                    file_name=file_name,
                    relative_path=relative_path,
                )
                document = document_ref_from_record(record)
                parsed = parse_document(document)
                classification = await classify_evidence_document(
                    document,
                    parsed,
                    self.models.evidence_classification,
                    self.settings.evidence_classification_model,
                )
                write_evidence_classification(
                    record.file_id,
                    classification,
                    self.settings,
                )
                classified_record = self.file_repository.update_classification(
                    record.file_id,
                    evidence_type=classification.evidence_type,
                    classification_status=classification.classification_status,
                )
                indexed_documents.append(
                    document_ref_from_record(classified_record)
                )

            await build_document_index(
                IndexScope(
                    scope_id=f"evidence-{project_id}-{bidder.bidder_id}",
                    data_role="evidence_material",
                    project_id=project_id,
                    bidder_id=bidder.bidder_id,
                ),
                indexed_documents,
                self.models.embedding,
                self.settings,
            )
            return self.bidder_repository.update_status(
                project_id, bidder.bidder_id, "evidence_ready"
            )
        except Exception as exc:
            self.bidder_repository.update_status(
                project_id,
                bidder.bidder_id,
                "evidence_failed",
                error_code=type(exc).__name__,
                error_message=str(exc),
            )
            raise
