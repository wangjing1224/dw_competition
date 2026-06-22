"""主审核 Agent 的投标检索、规则回查和资料核验 BaseTool。"""

from typing import Any, ClassVar

from langchain_core.tools import BaseTool, ToolException
from pydantic import BaseModel, ConfigDict, Field

from app.core.config import AppSettings
from app.models.documents import RetrievedSource, SourceRef
from app.models.evidence import Claim
from app.models.rules import RuleItem
from app.prompts.loader import load_tool_description
from app.retrieval.index import embedding_config_fingerprint
from app.retrieval.models import IndexFilter
from app.retrieval.service import RetrievalService
from app.retrieval.store import load_index
from app.storage.repositories import AuditTaskRepository, FileRepository


class SearchBidResponseInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    task_id: str
    bid_file_id: str
    rule: RuleItem


class RetrieveRuleSourceInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_id: str
    source: SourceRef


class RequestEvidenceReviewInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    claim: Claim


class SearchBidResponseTool(BaseTool):
    name: str = "search_bid_response"
    description: str = load_tool_description("search_bid_response")
    args_schema: ClassVar[type[BaseModel]] = SearchBidResponseInput
    model_config = ConfigDict(arbitrary_types_allowed=True)

    task_repository: AuditTaskRepository
    file_repository: FileRepository
    retrieval_service: RetrievalService

    async def _arun(
        self, task_id: str, bid_file_id: str, rule: RuleItem
    ) -> list[dict]:
        task = self.task_repository.require(task_id)
        file_record = self.file_repository.require(bid_file_id)
        if file_record.task_id != task_id:
            raise ToolException("投标文件不属于当前审核任务")
        results = await self.retrieval_service.search(
            f"{rule.name}\n{rule.requirement}\n{rule.target_field or ''}",
            IndexFilter(
                scope_id=f"bid-{task_id}",
                project_id=task.project_id,
                bidder_id=task.bidder_id,
                task_id=task_id,
                data_role="bid_document",
                file_ids=[bid_file_id],
            ),
        )
        return [item.model_dump(mode="json") for item in results]

    def _run(self, **_: object) -> object:
        raise ToolException("search_bid_response 仅支持异步调用")


class RetrieveRuleSourceTool(BaseTool):
    name: str = "retrieve_rule_source"
    description: str = load_tool_description("retrieve_rule_source")
    args_schema: ClassVar[type[BaseModel]] = RetrieveRuleSourceInput
    model_config = ConfigDict(arbitrary_types_allowed=True)

    settings: AppSettings

    def _run(self, project_id: str, source: SourceRef) -> dict:
        index = load_index(
            f"rules-{project_id}",
            expected_fingerprint=embedding_config_fingerprint(
                self.settings.embedding_model
            ),
        )
        chunk = next(
            (
                item
                for item in index.chunks
                if item.file_id == source.file_id
                and (source.chunk_id is None or item.chunk_id == source.chunk_id)
            ),
            None,
        )
        if chunk is None:
            raise ToolException("规则来源不属于当前项目或片段不存在")
        return RetrievedSource(
            source=chunk.source_ref(self.settings.retrieval.max_source_chars),
            content=chunk.content[: self.settings.retrieval.max_source_chars],
            parse_status=chunk.parse_status,
        ).model_dump(mode="json")

    async def _arun(self, project_id: str, source: SourceRef) -> dict:
        return self._run(project_id, source)


class RequestEvidenceReviewTool(BaseTool):
    name: str = "request_evidence_review"
    description: str = load_tool_description("request_evidence_review")
    args_schema: ClassVar[type[BaseModel]] = RequestEvidenceReviewInput
    model_config = ConfigDict(arbitrary_types_allowed=True)

    evidence_graph: Any

    async def _arun(self, claim: Claim) -> dict:
        final_state = await self.evidence_graph.ainvoke(
            {
                "messages": [],
                "task_id": claim.task_id,
                "project_id": claim.project_id,
                "bidder_id": claim.bidder_id,
                "claim": claim,
                "review_result": None,
                "errors": [],
                "step_count": 0,
            }
        )
        result = final_state.get("review_result")
        if result is None:
            raise ToolException("资料核验子图没有返回结果")
        return result.model_dump(mode="json") if hasattr(result, "model_dump") else result

    def _run(self, **_: object) -> object:
        raise ToolException("request_evidence_review 仅支持异步调用")
