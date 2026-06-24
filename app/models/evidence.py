"""资料分类目录、资料核验任务和轻量核验结果模型。"""

from pydantic import Field

from app.models.common import (
    ClassificationStatus,
    EvidenceReviewStatus,
    EvidenceType,
    StrictModel,
)


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


class EvidenceFileSummary(StrictModel):
    file_id: str
    evidence_summary: str


class EvidenceReviewTask(StrictModel):
    """子图内部状态；身份字段由系统维护，不暴露给子 Agent。"""

    task_id: str
    project_id: str
    bidder_id: str
    rule_id: str
    evidence_type: EvidenceType
    verification_request: str
    files: list[EvidenceFileSummary] = Field(default_factory=list)


class EvidenceReviewDecision(StrictModel):
    status: EvidenceReviewStatus
    summary: str = Field(min_length=2, max_length=500)
    evidence_chunk_ids: list[str] = Field(default_factory=list)


class EvidenceReviewResult(StrictModel):
    """资料核验工具返回给主 Agent 的外层结果。"""

    rule_id: str
    status: EvidenceReviewStatus
    summary: str = Field(min_length=2, max_length=500)
    evidence_chunk_ids: list[str] = Field(default_factory=list)
