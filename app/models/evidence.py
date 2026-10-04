"""独立资料库及入库模型；保留旧分类辅助函数使用的 DTO。审核子 Agent 直接返回正文。"""

from typing import Annotated, Literal

from pydantic import Field, StringConstraints, model_validator

from app.models.common import (
    ClassificationStatus,
    EvidenceType,
    StrictModel,
)

MaterialText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class EvidenceMaterialInput(StrictModel):
    """模型只整理当前文件；关系描述是自由文本，不是类型枚举。"""

    bid_summary: MaterialText = Field(
        description="简洁说明当前文件的对象、可查事项及读取限制，不放图片路径，不下审核结论。"
    )
    evidence_summary: MaterialText = Field(
        description="当前文件完整详细摘要：关键事实、对象编号、范围、局部读取限制及关联依据；图片事实附真实登记路径，不含阅读批次等运行状态。"
    )
    relations: list[tuple[MaterialText, MaterialText]] = Field(
        description="二元组列表：[简短关系短语, 已完成历史文件ID]。第一项是自由文本而非枚举，具体关联依据写入 evidence_summary；无关系时填 []。"
    )


class EvidenceMaterial(EvidenceMaterialInput):
    file_id: str
    # 系统维护的编辑来源，不进入模型工具参数或业务文本视图。
    provenance: Literal["ai_generated", "human_modified"] = "ai_generated"


class EvidenceSourceFile(StrictModel):
    file_id: str
    file_name: str
    source_path: str


class EvidenceLibrary(StrictModel):
    bidder_id: str
    source_files: list[EvidenceSourceFile] = Field(default_factory=list)
    materials: list[EvidenceMaterial] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_files(self):
        sources = [source.file_id for source in self.source_files]
        ids = [item.file_id for item in self.materials]
        if len(set(sources)) != len(sources) or len(set(ids)) != len(ids):
            raise ValueError("资料文件 ID 必须唯一")
        if not set(ids) <= set(sources):
            raise ValueError("资料结果必须来自本次登记文件")
        for item in self.materials:
            if any(
                target == item.file_id or target not in ids
                for _, target in item.relations
            ):
                raise ValueError("关系目标必须是其他已有资料记录")
        return self


class SaveEvidenceMaterialInput(StrictModel):
    material: EvidenceMaterialInput | None = Field(
        description="写入当前文件的完整双摘要和关系，未改变的事实也要保留；只读时显式 null。"
    )
    file_ids: list[MaterialText] | Literal["all"] | None = Field(
        description='只读时填已完成历史文件 ID 列表或字符串 "all"；写入时显式 null。'
    )

    @model_validator(mode="after")
    def exclusive_mode(self):
        if (self.material is None) == (self.file_ids is None):
            raise ValueError("material、file_ids 必须恰好一项为 null")
        if isinstance(self.file_ids, str) and self.file_ids != "all":
            raise ValueError('字符串范围只允许 "all"')
        if self.file_ids == []:
            raise ValueError("文件列表不能为空")
        return self


class EvidenceIngestionProgress(StrictModel):
    """执行状态，不重复保存资料业务结果。恢复仅限同一文件清单和解析版本。"""

    bidder_id: str
    file_ids: list[str]
    parser_versions: dict[str, str]
    completed_file_ids: list[str] = Field(default_factory=list)
    current_file_id: str | None = None
    next_unit_index: int = 0
    batch_index: int = 0
    model_calls: int = 0
    file_model_calls: int = 0
    compaction_calls: int = 0
    compaction_records: list[dict] = Field(default_factory=list)
    error: str | None = None
    unresolved_write_error: str | None = None


class EvidenceClassification(StrictModel):
    evidence_type: EvidenceType
    classification_status: ClassificationStatus
    reason: str = Field(min_length=2, max_length=500)
    bid_summary: str = Field(min_length=2, max_length=500)
    evidence_summary: str = Field(min_length=2, max_length=2000)


class EvidenceClassificationRecord(EvidenceClassification):
    """代码补充文件 ID 后保存到最终结果，不进入主 Agent 输入。"""

    file_id: str
    file_name: str


class EvidenceCatalogItem(StrictModel):
    file_id: str
    status: ClassificationStatus
    summary: str
