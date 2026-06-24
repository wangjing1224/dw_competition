"""文件引用、来源引用和统一文档解析结果模型。"""

from pydantic import Field, model_validator

from app.models.common import (
    ClassificationStatus,
    DataRole,
    EvidenceType,
    ParseStatus,
    StrictModel,
)


class DocumentRef(StrictModel):
    file_id: str
    file_name: str
    source_path: str
    data_role: DataRole
    parse_status: ParseStatus = ParseStatus.PENDING
    project_id: str
    bidder_id: str | None = None
    task_id: str | None = None
    evidence_type: EvidenceType | None = None
    classification_status: ClassificationStatus | None = None

    @model_validator(mode="after")
    def validate_ownership(self) -> "DocumentRef":
        if self.data_role == DataRole.RULE_SOURCE:
            if self.bidder_id or self.task_id:
                raise ValueError("规则文件不能绑定 bidder_id 或 task_id")
        elif self.data_role == DataRole.EVIDENCE_MATERIAL:
            if not self.bidder_id or self.task_id:
                raise ValueError("资料文件必须绑定 bidder_id 且不能绑定 task_id")
        elif not self.bidder_id or not self.task_id:
            raise ValueError("投标文件必须绑定 bidder_id 和 task_id")
        return self


def document_ref_from_record(record: object) -> DocumentRef:
    """把存储层 FileRecord 转为不暴露 storage_path 的领域文件引用。"""

    return DocumentRef(
        file_id=getattr(record, "file_id"),
        file_name=getattr(record, "file_name"),
        source_path=getattr(record, "source_path"),
        data_role=getattr(record, "data_role"),
        parse_status=getattr(record, "parse_status"),
        project_id=getattr(record, "project_id"),
        bidder_id=getattr(record, "bidder_id"),
        task_id=getattr(record, "task_id"),
        evidence_type=getattr(record, "evidence_type"),
        classification_status=getattr(record, "classification_status"),
    )


class SourceRef(StrictModel):
    file_id: str
    chunk_id: str | None = None
    file_name: str
    source_path: str
    section: str | None = None
    quote: str | None = None


class RetrievedSource(StrictModel):
    source: SourceRef
    content: str
    tables: list[str] = Field(default_factory=list)
    parse_status: ParseStatus


class ParsedSection(StrictModel):
    section_id: str
    title: str
    content: str = ""
    tables: list[str] = Field(default_factory=list)
    image_count: int = Field(default=0, ge=0)
    source_block_index: int | None = Field(default=None, ge=0)


class ParsedDocument(StrictModel):
    document: DocumentRef
    parse_status: ParseStatus
    sections: list[ParsedSection] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class BidRouteDecision(StrictModel):
    """摘要模型只输出路由状态、原因和详细摘要。"""

    match_status: str
    match_reason: str
    summary: str

    @model_validator(mode="after")
    def validate_match_status(self) -> "BidRouteDecision":
        if self.match_status not in {"matched", "not_matched", "uncertain"}:
            raise ValueError("match_status 必须为 matched/not_matched/uncertain")
        return self


class BidDocumentSummary(BidRouteDecision):
    """代码补充文件身份后的持久化摘要。"""

    file_id: str
