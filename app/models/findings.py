"""规则级最终审核结论。"""

from pydantic import Field

from app.models.common import FindingResult, StrictModel


class Finding(StrictModel):
    rule_id: str
    result: FindingResult
    summary: str
    observation: str | None = None
    recommendation: str | None = None
    tender_chunk_ids: list[str] = Field(default_factory=list)
    bid_chunk_ids: list[str] = Field(default_factory=list)
    evidence_chunk_ids: list[str] = Field(default_factory=list)
    image_paths: list[str] = Field(default_factory=list)


class ReportHighlight(StrictModel):
    title: str = Field(min_length=1)
    rule_ids: list[str] = Field(min_length=1)
    summary: str = Field(min_length=1)
