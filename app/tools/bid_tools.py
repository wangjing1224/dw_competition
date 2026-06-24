"""主审核 Agent 的投标检索、招标原文检索和资料核验工具。"""

from typing import Annotated, Any, ClassVar

from langchain_core.tools import BaseTool, ToolException
from langgraph.prebuilt import InjectedState
from pydantic import BaseModel, ConfigDict, Field

from app.core.config import AppSettings
from app.evidence.storage import load_evidence_classification
from app.models.common import EvidenceType
from app.models.evidence import (
    EvidenceFileSummary,
    EvidenceReviewResult,
    EvidenceReviewTask,
)
from app.prompts.loader import load_tool_description
from app.retrieval.models import IndexFilter, SearchResult
from app.retrieval.service import RetrievalService
from app.storage.repositories import AuditEventRepository


class SearchQueriesInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    queries: list[str] = Field(min_length=1, max_length=5)
    state: Annotated[dict, InjectedState]


class EvidenceReviewToolInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    rule_id: str
    verification_request: str
    evidence_type: EvidenceType
    state: Annotated[dict, InjectedState]


def _compact_results(
    results: list[SearchResult],
    *,
    per_chunk_chars: int,
    total_chars: int,
    top_k: int,
) -> dict[str, list[dict[str, str]]]:
    chunks: list[dict[str, str]] = []
    used = 0
    seen: set[str] = set()
    for result in sorted(results, key=lambda item: item.score, reverse=True):
        if result.chunk_id in seen or len(chunks) >= top_k:
            continue
        remaining = total_chars - used
        if remaining <= 0:
            break
        content = result.content[: min(per_chunk_chars, remaining)]
        chunks.append(
            {
                "chunk_id": result.chunk_id,
                "file_id": result.source.file_id,
                "section": result.source.section or "",
                "content": content,
            }
        )
        seen.add(result.chunk_id)
        used += len(content)
    return {"chunks": chunks}


async def _search_many(
    retrieval: RetrievalService,
    queries: list[str],
    query_filter: IndexFilter,
) -> list[SearchResult]:
    merged: list[SearchResult] = []
    for query in dict.fromkeys(item.strip() for item in queries if item.strip()):
        merged.extend(
            await retrieval.search(
                query,
                query_filter,
                candidate_top_k=20,
                final_top_k=8,
            )
        )
    return merged


class SearchBidResponseTool(BaseTool):
    name: str = "search_bid_response"
    description: str = load_tool_description("search_bid_response")
    args_schema: ClassVar[type[BaseModel]] = SearchQueriesInput
    model_config = ConfigDict(arbitrary_types_allowed=True)

    retrieval_service: RetrievalService
    settings: AppSettings
    event_repository: AuditEventRepository

    async def _arun(
        self,
        queries: list[str],
        state: Annotated[dict, InjectedState],
    ) -> dict:
        bid_file = state["bid_files"][state["current_bid_index"]]
        results = await _search_many(
            self.retrieval_service,
            queries,
            IndexFilter(
                scope_id=f"bid-{state['task_id']}",
                project_id=state["project_id"],
                bidder_id=state["bidder_id"],
                task_id=state["task_id"],
                data_role="bid_document",
                file_ids=[bid_file.file_id],
            ),
        )
        response = _compact_results(
            results,
            per_chunk_chars=min(self.settings.retrieval.max_source_chars, 1800),
            total_chars=min(self.settings.retrieval.max_source_chars * 2, 8000),
            top_k=5,
        )
        self.event_repository.append(
            state["task_id"],
            actor="tool",
            event_type="tool_result",
            title="检索投标文件原文",
            detail={
                "tool": self.name,
                "file_id": bid_file.file_id,
                "query_count": len(queries),
                "result_count": len(response["chunks"]),
            },
        )
        return response

    def _run(self, **_: object) -> object:
        raise ToolException("search_bid_response 仅支持异步调用")


class SearchTenderSourceTool(BaseTool):
    name: str = "search_tender_source"
    description: str = load_tool_description("search_tender_source")
    args_schema: ClassVar[type[BaseModel]] = SearchQueriesInput
    model_config = ConfigDict(arbitrary_types_allowed=True)

    retrieval_service: RetrievalService
    settings: AppSettings
    event_repository: AuditEventRepository

    async def _arun(
        self,
        queries: list[str],
        state: Annotated[dict, InjectedState],
    ) -> dict:
        results = await _search_many(
            self.retrieval_service,
            queries,
            IndexFilter(
                scope_id=f"rules-{state['project_id']}",
                project_id=state["project_id"],
                data_role="rule_source",
            ),
        )
        response = _compact_results(
            results,
            per_chunk_chars=min(self.settings.retrieval.max_source_chars, 1800),
            total_chars=min(self.settings.retrieval.max_source_chars * 2, 8000),
            top_k=5,
        )
        self.event_repository.append(
            state["task_id"],
            actor="tool",
            event_type="tool_result",
            title="检索招标文件原文",
            detail={
                "tool": self.name,
                "query_count": len(queries),
                "result_count": len(response["chunks"]),
            },
        )
        return response

    def _run(self, **_: object) -> object:
        raise ToolException("search_tender_source 仅支持异步调用")


class RequestEvidenceReviewTool(BaseTool):
    name: str = "request_evidence_review"
    description: str = load_tool_description("request_evidence_review")
    args_schema: ClassVar[type[BaseModel]] = EvidenceReviewToolInput
    model_config = ConfigDict(arbitrary_types_allowed=True)

    evidence_graph: Any
    settings: AppSettings
    event_repository: AuditEventRepository

    async def _arun(
        self,
        rule_id: str,
        verification_request: str,
        evidence_type: EvidenceType,
        state: Annotated[dict, InjectedState],
    ) -> dict:
        rule_ids = {rule.rule_id for rule in state["rule_set"].rules}
        if rule_id not in rule_ids:
            raise ToolException("资料核验引用了未知规则")
        catalog_items = state["evidence_catalog"].get(str(evidence_type), [])
        available = [
            item for item in catalog_items if str(item.status) == "classified"
        ]
        if not available:
            return EvidenceReviewResult(
                rule_id=rule_id,
                status="unsupported",
                summary="当前资料目录中没有可自动读取的对应资料，需结合人工核验情况判断",
                evidence_chunk_ids=[],
            ).model_dump(mode="json")

        files: list[EvidenceFileSummary] = []
        for item in available:
            classification = load_evidence_classification(
                item.file_id,
                self.settings,
            )
            if classification is not None:
                files.append(
                    EvidenceFileSummary(
                        file_id=item.file_id,
                        evidence_summary=classification.evidence_summary,
                    )
                )
        if not files:
            return EvidenceReviewResult(
                rule_id=rule_id,
                status="unverifiable",
                summary="资料目录存在对应文件，但缺少可供子 Agent 使用的文件级摘要",
                evidence_chunk_ids=[],
            ).model_dump(mode="json")
        review_task = EvidenceReviewTask(
            task_id=state["task_id"],
            project_id=state["project_id"],
            bidder_id=state["bidder_id"],
            rule_id=rule_id,
            evidence_type=evidence_type,
            verification_request=verification_request,
            files=files,
        )
        self.event_repository.append(
            state["task_id"],
            actor="tool",
            event_type="subagent_started",
            title="主 Agent 委派资料核验任务",
            detail={
                "tool": self.name,
                "rule_id": rule_id,
                "evidence_type": str(evidence_type),
                "file_count": len(files),
            },
        )
        final_state = await self.evidence_graph.ainvoke(
            {
                "messages": [],
                "task_id": state["task_id"],
                "project_id": state["project_id"],
                "bidder_id": state["bidder_id"],
                "review_task": review_task,
                "review_decision": None,
                "errors": [],
                "step_count": 0,
            },
            config={
                "run_name": "evidence_review_agent",
                "tags": ["audit", "evidence-agent"],
                "metadata": {
                    "task_id": state["task_id"],
                    "rule_id": rule_id,
                    "evidence_type": str(evidence_type),
                },
            },
        )
        decision = final_state.get("review_decision")
        if decision is None:
            raise ToolException("资料核验子图没有返回结果")
        result = EvidenceReviewResult(
            rule_id=rule_id,
            status=decision.status,
            summary=decision.summary,
            evidence_chunk_ids=decision.evidence_chunk_ids,
        )
        self.event_repository.append(
            state["task_id"],
            actor="tool",
            event_type="subagent_completed",
            title="资料核验结果返回主 Agent",
            detail={
                "tool": self.name,
                "rule_id": rule_id,
                "status": str(result.status),
                "evidence_count": len(result.evidence_chunk_ids),
            },
        )
        return result.model_dump(mode="json")

    def _run(self, **_: object) -> object:
        raise ToolException("request_evidence_review 仅支持异步调用")
