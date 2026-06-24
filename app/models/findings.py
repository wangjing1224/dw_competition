"""规则级最终审核结论。"""

from pydantic import Field

from app.models.common import FindingResult, StrictModel


class Finding(StrictModel):
    rule_id: str
    result: FindingResult
    summary: str
    tender_chunk_ids: list[str] = Field(default_factory=list)
    bid_chunk_ids: list[str] = Field(default_factory=list)
    evidence_chunk_ids: list[str] = Field(default_factory=list)
