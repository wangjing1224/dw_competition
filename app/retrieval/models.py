"""索引范围、片段、过滤条件、清单和检索结果模型。"""

from typing import Literal

from pydantic import Field, model_validator

from app.models.common import (
    ClassificationStatus,
    DataRole,
    EvidenceType,
    ParseStatus,
    StrictModel,
)
from app.models.documents import SourceRef


class IndexScope(StrictModel):
    scope_id: str
    data_role: DataRole
    project_id: str
    bidder_id: str | None = None
    task_id: str | None = None

    @model_validator(mode="after")
    def validate_scope(self) -> "IndexScope":
        if self.data_role == DataRole.RULE_SOURCE:
            if self.bidder_id or self.task_id:
                raise ValueError("规则索引不能绑定 bidder 或 task")
        elif self.data_role == DataRole.EVIDENCE_MATERIAL:
            if not self.bidder_id or self.task_id:
                raise ValueError("资料索引必须绑定 bidder 且不能绑定 task")
        elif not self.bidder_id or not self.task_id:
            raise ValueError("投标索引必须绑定 bidder 和 task")
        return self


class IndexChunk(StrictModel):
    chunk_id: str
    file_id: str
    project_id: str
    bidder_id: str | None = None
    task_id: str | None = None
    data_role: DataRole
    file_name: str
    source_path: str
    section: str
    content: str
    retrieval_hint: str | None = None
    parse_status: ParseStatus
    evidence_type: EvidenceType | None = None
    classification_status: ClassificationStatus | None = None
    block_kind: Literal["text", "table", "image"] = "text"
    source_block_index: int | None = None
    parse_version: str | None = None
    page_numbers: list[int] = Field(default_factory=list)
    image_paths: list[str] = Field(default_factory=list)

    def source_ref(self, quote_chars: int = 500) -> SourceRef:
        return SourceRef(
            file_id=self.file_id,
            chunk_id=self.chunk_id,
            file_name=self.file_name,
            source_path=self.source_path,
            section=self.section,
            quote=self.content[:quote_chars],
        )


class IndexFilter(StrictModel):
    scope_id: str
    project_id: str
    data_role: DataRole
    bidder_id: str | None = None
    task_id: str | None = None
    file_ids: list[str] = Field(default_factory=list)
    evidence_types: list[EvidenceType] = Field(default_factory=list)


class IndexManifest(StrictModel):
    version: Literal["1.0"] = "1.0"
    status: Literal["ready"] = "ready"
    dimension: int = Field(gt=0)
    chunk_count: int = Field(ge=0)
    embedding_model: str
    embedding_fingerprint: str


class RecallItem(StrictModel):
    chunk: IndexChunk
    semantic_score: float | None = None
    keyword_score: float | None = None
    rerank_score: float | None = None


class SearchResult(StrictModel):
    chunk_id: str
    score: float
    source: SourceRef
    content: str
    project_id: str
    bidder_id: str | None = None
    task_id: str | None = None
    evidence_type: EvidenceType | None = None
    parse_version: str | None = None
    page_numbers: list[int] = Field(default_factory=list)
    image_paths: list[str] = Field(default_factory=list)
    truncated: bool = False
