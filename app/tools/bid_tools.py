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
from app.storage.repositories import (
    AuditEventRepository,
    AuditTaskRepository,
    FileRepository,
)
from app.storage.ids import next_id


class SearchBidResponseInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    task_id: str
    bid_file_id: str
    rule: RuleItem


class RetrieveRuleSourceInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    task_id: str
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
    event_repository: AuditEventRepository

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
            candidate_top_k=20,
            final_top_k=5,
        )
        self.event_repository.append(
            task_id,
            actor="tool",
            event_type="tool_result",
            title="检索投标响应完成",
            detail={
                "tool": self.name,
                "rule_id": rule.rule_id,
                "rule_name": rule.name,
                "result_count": len(results),
                "top_results": [
                    {
                        "file_id": item.source.file_id,
                        "section": item.source.section,
                        "score": item.score,
                    }
                    for item in results[:5]
                ],
            },
        )
        return [
            item.model_copy(update={"content": item.content[:1500]}).model_dump(
                mode="json"
            )
            for item in results
        ]

    def _run(self, **_: object) -> object:
        raise ToolException("search_bid_response 仅支持异步调用")


class RetrieveRuleSourceTool(BaseTool):
    name: str = "retrieve_rule_source"
    description: str = load_tool_description("retrieve_rule_source")
    args_schema: ClassVar[type[BaseModel]] = RetrieveRuleSourceInput
    model_config = ConfigDict(arbitrary_types_allowed=True)

    settings: AppSettings
    event_repository: AuditEventRepository

    def _run(self, task_id: str, project_id: str, source: SourceRef) -> dict:
        task = AuditTaskRepository().require(task_id)
        if task.project_id != project_id:
            raise ToolException("审核任务不属于当前项目")
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
        self.event_repository.append(
            task_id,
            actor="tool",
            event_type="tool_result",
            title="读取招标规则原文片段",
            detail={
                "tool": self.name,
                "file_id": source.file_id,
                "chunk_id": chunk.chunk_id,
                "section": chunk.section,
                "content_chars": min(
                    len(chunk.content),
                    self.settings.retrieval.max_source_chars,
                ),
            },
        )
        return RetrievedSource(
            source=chunk.source_ref(self.settings.retrieval.max_source_chars),
            content=chunk.content[: self.settings.retrieval.max_source_chars],
            parse_status=chunk.parse_status,
        ).model_dump(mode="json")

    async def _arun(
        self,
        task_id: str,
        project_id: str,
        source: SourceRef,
    ) -> dict:
        return self._run(task_id, project_id, source)


class RequestEvidenceReviewTool(BaseTool):
    name: str = "request_evidence_review"
    description: str = load_tool_description("request_evidence_review")
    args_schema: ClassVar[type[BaseModel]] = RequestEvidenceReviewInput
    model_config = ConfigDict(arbitrary_types_allowed=True)

    evidence_graph: Any
    event_repository: AuditEventRepository

    async def _arun(self, claim: Claim) -> dict:
        # Claim 的业务内容来自主 Agent，但 ID 由程序统一缩短和持久化分配。
        claim = claim.model_copy(update={"claim_id": next_id("claim")})
        self.event_repository.append(
            claim.task_id,
            actor="tool",
            event_type="subagent_started",
            title="主 Agent 委派资料核验任务",
            detail={
                "tool": self.name,
                "claim_id": claim.claim_id,
                "rule_id": claim.rule_id,
                "statement": claim.statement[:300],
                "required_evidence_types": [
                    str(item) for item in claim.required_evidence_types
                ],
            },
        )
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
            },
            config={
                "run_name": "evidence_review_agent",
                "tags": ["audit", "evidence-agent"],
                "metadata": {
                    "task_id": claim.task_id,
                    "project_id": claim.project_id,
                    "bidder_id": claim.bidder_id,
                    "claim_id": claim.claim_id,
                    "rule_id": claim.rule_id,
                },
            },
        )
        result = final_state.get("review_result")
        if result is None:
            raise ToolException("资料核验子图没有返回结果")
        self.event_repository.append(
            claim.task_id,
            actor="tool",
            event_type="subagent_completed",
            title="资料核验子任务返回主 Agent",
            detail={
                "tool": self.name,
                "claim_id": claim.claim_id,
                "status": str(result.status),
                "summary": result.summary,
            },
        )
        return result.model_dump(mode="json") if hasattr(result, "model_dump") else result

    def _run(self, **_: object) -> object:
        raise ToolException("request_evidence_review 仅支持异步调用")
