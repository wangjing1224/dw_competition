"""文件引用、来源引用和统一文档解析结果模型。"""

import re
from pathlib import PurePosixPath
from typing import Literal

from pydantic import Field, field_validator, model_validator

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
    project_id: str | None = None
    bidder_id: str | None = None
    task_id: str | None = None
    evidence_type: EvidenceType | None = None
    classification_status: ClassificationStatus | None = None

    @model_validator(mode="after")
    def validate_ownership(self) -> "DocumentRef":
        if self.data_role == DataRole.RULE_SOURCE:
            if not self.project_id or self.bidder_id or self.task_id:
                raise ValueError(
                    "规则文件必须绑定 project_id，不能绑定 bidder_id 或 task_id"
                )
        elif self.data_role == DataRole.EVIDENCE_MATERIAL:
            if not self.bidder_id or self.task_id or self.project_id:
                raise ValueError(
                    "资料文件只绑定 bidder_id，不能绑定 project_id 或 task_id"
                )
        elif not self.project_id or not self.bidder_id or not self.task_id:
            raise ValueError("投标文件必须绑定 project_id、bidder_id 和 task_id")
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


def validate_asset_path(value: str) -> str:
    """图片统一相对 parsed 根目录寻址：{file_id}/images/{文件名}。"""
    path = PurePosixPath(value)
    if (
        not value
        or path.is_absolute()
        or ".." in path.parts
        or "\\" in value
        or ":" in value
        or path.as_posix() != value
    ):
        raise ValueError("图片路径必须是文件内的安全相对路径")
    if value.startswith("images/"):
        raise ValueError("旧图片路径缺少 file_id，请重新解析原文件")
    if (
        len(path.parts) != 3
        or not re.fullmatch(r"[A-Za-z0-9_-]+", path.parts[0])
        or path.parts[1] != "images"
    ):
        raise ValueError("图片路径必须为 {file_id}/images/{文件名}")
    return value


class ParsedImage(StrictModel):
    path: str
    mime_type: Literal["image/png", "image/jpeg"]
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    description: str = ""
    transcription: str = ""
    # failed 是程序处理失败；unreadable 是模型返回的读取结论，二者不混用。
    read_status: Literal[
        "pending", "readable", "partial", "unreadable", "skipped", "failed"
    ] = "pending"
    # 回查截图不自动转写，防止把原生表格再入库一遍。
    purpose: Literal["content", "reference"] = "content"
    warning: str | None = None

    _validate_path = field_validator("path")(validate_asset_path)


class PageSource(StrictModel):
    page_number: int = Field(ge=1)
    # PDF point，左上原点，使用显示方向坐标。
    bbox: tuple[float, float, float, float] | None = None
    image_path: str | None = None

    @field_validator("image_path")
    @classmethod
    def validate_image_path(cls, value):
        return validate_asset_path(value) if value is not None else None

    @field_validator("bbox")
    @classmethod
    def validate_bbox(cls, value):
        import math

        if value is not None and (
            not all(math.isfinite(n) for n in value)
            or value[2] <= value[0]
            or value[3] <= value[1]
        ):
            raise ValueError("页面区域必须是有效矩形")
        return value


class ParsedBlock(StrictModel):
    kind: Literal["text", "table", "image"]
    content: str = ""
    content_origin: Literal["native", "vision_assisted"] = "native"
    images: list[ParsedImage] = Field(default_factory=list)
    source_block_index: int = Field(ge=0)
    page_sources: list[PageSource] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_structure(self):
        if self.kind == "image" and not self.images:
            raise ValueError("图片块不能为空")
        available = {image.path for image in self.images}
        if any(
            source.image_path and source.image_path not in available
            for source in self.page_sources
        ):
            raise ValueError("页面回查图必须登记在当前块中")
        return self


class ParsedSection(StrictModel):
    section_id: str
    title: str
    content: str = ""
    tables: list[str] = Field(default_factory=list)
    image_count: int = Field(default=0, ge=0)
    source_block_index: int | None = Field(default=None, ge=0)
    blocks: list[ParsedBlock] = Field(default_factory=list)

    @model_validator(mode="after")
    def project_legacy_fields(self):
        # 新块是唯一事实源；旧消费端暂时仍读取 content/tables，不能反向拼块。
        if self.blocks:
            self.project_blocks()
        return self

    def project_blocks(self) -> None:
        """v2 显式投影，空块也清空旧字段；v1 不应调用此方法。"""
        text = [b.content for b in self.blocks if b.kind == "text" and b.content]
        text.extend(
            i.transcription for b in self.blocks for i in b.images if i.transcription
        )
        object.__setattr__(self, "content", "\n".join(text))
        object.__setattr__(
            self,
            "tables",
            [b.content for b in self.blocks if b.kind == "table" and b.content],
        )
        object.__setattr__(self, "image_count", sum(len(b.images) for b in self.blocks))


class ParsedDocument(StrictModel):
    # 无版本的历史 JSON 按 v1 读取，不伪造已丢失的顺序和图片引用。
    schema_version: Literal[1, 2] = 1
    parser_version: str | None = None
    source_sha256: str | None = None
    markdown_path: str | None = None
    document: DocumentRef
    parse_status: ParseStatus
    sections: list[ParsedSection] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def project_v2_sections(self):
        for section in self.sections:
            for block in section.blocks:
                for image in block.images:
                    if PurePosixPath(image.path).parts[0] != self.document.file_id:
                        raise ValueError("图片路径的 file_id 与当前解析文件不一致")
        if self.schema_version == 2:
            for section in self.sections:
                section.project_blocks()
        return self


class BidRouteDecision(StrictModel):
    """路由只判断项目归属；审核直接读取解析原文。"""

    match_status: str
    match_reason: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_match_status(self) -> "BidRouteDecision":
        if self.match_status not in {"matched", "not_matched", "uncertain"}:
            raise ValueError("match_status 必须为 matched/not_matched/uncertain")
        return self


class BidDocumentSummary(BidRouteDecision):
    """沿用存储名称，只保留路由记录与代码维护的阅读范围。"""

    file_id: str
    reading_end: int = 0
    total_units: int = 0
