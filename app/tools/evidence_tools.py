"""资料核验子 Agent 的候选检索和原文回查 BaseTool。"""

from typing import ClassVar

from langchain_core.tools import BaseTool, ToolException
from pydantic import BaseModel, ConfigDict, Field

from app.models.documents import RetrievedSource
from app.models.evidence import Claim, EvidenceCandidate
from app.prompts.loader import load_tool_description
from app.retrieval.models import IndexFilter
from app.retrieval.service import RetrievalService
from app.retrieval.store import load_index
from app.retrieval.index import embedding_config_fingerprint
from app.core.config import AppSettings


class SearchEvidenceInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    claim: Claim = Field(description="主 Agent 生成的单条受信任待核验声明")


class RetrieveEvidenceSourceInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    claim: Claim
    candidate: EvidenceCandidate


class SearchEvidenceTool(BaseTool):
    name: str = "search_evidence"
    description: str = load_tool_description("search_evidence")
    args_schema: ClassVar[type[BaseModel]] = SearchEvidenceInput
    model_config = ConfigDict(arbitrary_types_allowed=True)

    retrieval_service: RetrievalService

    async def _arun(self, claim: Claim) -> list[dict]:
        results = await self.retrieval_service.search(
            claim.statement,
            IndexFilter(
                scope_id=f"evidence-{claim.project_id}-{claim.bidder_id}",
                project_id=claim.project_id,
                bidder_id=claim.bidder_id,
                data_role="evidence_material",
                evidence_types=claim.required_evidence_types,
            ),
        )
        return [
            EvidenceCandidate(
                file_id=item.source.file_id,
                chunk_id=item.chunk_id,
                project_id=item.project_id,
                bidder_id=item.bidder_id or claim.bidder_id,
                evidence_type=item.evidence_type or "other",
                score=item.score,
                summary=item.content[:500],
                source=item.source,
            ).model_dump(mode="json")
            for item in results
        ]

    def _run(self, **_: object) -> object:
        raise ToolException("search_evidence 仅支持异步调用")


class RetrieveEvidenceSourceTool(BaseTool):
    name: str = "retrieve_evidence_source"
    description: str = load_tool_description("retrieve_evidence_source")
    args_schema: ClassVar[type[BaseModel]] = RetrieveEvidenceSourceInput
    model_config = ConfigDict(arbitrary_types_allowed=True)

    settings: AppSettings

    def _run(self, claim: Claim, candidate: EvidenceCandidate) -> dict:
        # Claim 受信任，但候选仍必须属于 Claim 指定的投标方和项目。
        if (
            candidate.project_id != claim.project_id
            or candidate.bidder_id != claim.bidder_id
        ):
            raise ToolException("候选资料不属于当前 Claim")
        index = load_index(
            f"evidence-{claim.project_id}-{claim.bidder_id}",
            expected_fingerprint=embedding_config_fingerprint(
                self.settings.embedding_model
            ),
        )
        chunk = next(
            (
                item
                for item in index.chunks
                if item.chunk_id == candidate.chunk_id
                and item.file_id == candidate.file_id
            ),
            None,
        )
        if chunk is None:
            raise ToolException("候选资料片段不存在")
        return RetrievedSource(
            source=chunk.source_ref(self.settings.retrieval.max_source_chars),
            content=chunk.content[: self.settings.retrieval.max_source_chars],
            parse_status=chunk.parse_status,
        ).model_dump(mode="json")

    async def _arun(self, claim: Claim, candidate: EvidenceCandidate) -> dict:
        return self._run(claim, candidate)
