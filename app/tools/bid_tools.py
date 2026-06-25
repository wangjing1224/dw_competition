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


class BidSearchCheck(BaseModel):
    """投标原文中的单个待核验事实。"""

    model_config = ConfigDict(extra="forbid")
    topic: str = Field(
        min_length=2,
        max_length=100,
        description=(
            "简短描述一个待核验的投标响应事实，例如“投标报价”或“投标有效期”。"
            "不要粘贴整条规则，也不要在一个topic中混入多个无关事项。"
        ),
    )
    queries: list[str] = Field(
        min_length=1,
        max_length=5,
        description=(
            "围绕当前topic提供1至5个互补查询，可分别定位投标函、附录、报价表或技术"
            "响应表。不得仅改写同义词重复查询已经没有新召回的事实。"
        ),
    )


class SearchBidResponseInput(BaseModel):
    """主 Agent 检索当前投标文件时的模型可见输入。"""

    model_config = ConfigDict(extra="forbid")
    checks: list[BidSearchCheck] = Field(
        min_length=1,
        max_length=8,
        description=(
            "本轮需要在当前投标文件中核验的1至8个独立事实。每个check用topic说明"
            "一个审核问题，并提供1至5个搜索不同章节或表述的互补query。"
        ),
    )
    state: Annotated[dict, InjectedState]


class TenderSearchCheck(BaseModel):
    """招标原文中的单个待确认要求。"""

    model_config = ConfigDict(extra="forbid")
    topic: str = Field(
        min_length=2,
        max_length=100,
        description=(
            "简短描述一个待确认的招标要求，例如“投标保证金适用性”或“分辨率指标”。"
            "不要查询投标方如何响应，也不要粘贴整条规则。"
        ),
    )
    queries: list[str] = Field(
        min_length=1,
        max_length=5,
        description=(
            "围绕当前topic提供1至5个互补查询，应包含条款主题、适用范围、资格条件、"
            "技术指标、商务条件或否决条件。"
        ),
    )


class SearchTenderSourceInput(BaseModel):
    """主 Agent 检索招标文件原文时的模型可见输入。"""

    model_config = ConfigDict(extra="forbid")
    checks: list[TenderSearchCheck] = Field(
        min_length=1,
        max_length=8,
        description=(
            "本轮需要回查的1至8个独立招标要求。每个check用topic说明一个要求，"
            "并提供1至5个搜索不同章节或表述的互补query。"
        ),
    )
    state: Annotated[dict, InjectedState]


class EvidenceReviewCheck(BaseModel):
    """主 Agent 委派给资料子 Agent 的单个细粒度核验事项。"""

    model_config = ConfigDict(extra="forbid")
    rule_id: str = Field(
        description="需要核验的规则唯一标识，必须来自当前输入规则，例如rule-32。",
    )
    verification_request: str = Field(
        min_length=2,
        max_length=500,
        description=(
            "一句话说明当前资料类别内需要核验的单个明确事实及通过标准。"
            "不得在一个check中混入其他资料类别负责的事项。"
        ),
    )
    evidence_type: EvidenceType = Field(
        description=(
            "当前check唯一允许访问的资料类别。必须来自资料目录中存在且至少有一个"
            "classified文件的类别；同一规则需要多类资料时拆成多个check。"
        ),
    )


class EvidenceReviewToolInput(BaseModel):
    """主 Agent 委派资料核验时的模型可见输入。"""

    model_config = ConfigDict(extra="forbid")
    checks: list[EvidenceReviewCheck] = Field(
        min_length=1,
        max_length=10,
        description=(
            "本轮需要委派的1至10个独立资料核验事项。每个check只对应一个rule_id、"
            "一个evidence_type和一个明确事实；同一规则可以包含多个不同资料类别的check。"
        ),
    )
    state: Annotated[dict, InjectedState]


def _compact_grouped_results(
    grouped_results: list[tuple[str, list[SearchResult]]],
    *,
    per_chunk_chars: int,
    total_chars: int,
    per_topic_top_k: int = 2,
) -> dict[str, list[dict[str, object]]]:
    """按核验事实分配片段配额，避免全局 Top-K 挤掉其他主题。"""

    response: list[dict[str, object]] = []
    global_seen: set[str] = set()
    topic_budget = max(600, total_chars // max(len(grouped_results), 1))
    for topic, results in grouped_results:
        chunks: list[dict[str, str]] = []
        topic_seen: set[str] = set()
        topic_used = 0
        ranked = sorted(results, key=lambda item: item.score, reverse=True)
        candidates = [
            result for result in ranked if result.chunk_id not in global_seen
        ]
        # 所有高分结果都被其他主题使用时，允许复用最相关片段，避免当前主题无结果。
        if not candidates:
            candidates = ranked
        for result in candidates:
            if result.chunk_id in topic_seen or len(chunks) >= per_topic_top_k:
                continue
            remaining = topic_budget - topic_used
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
            topic_seen.add(result.chunk_id)
            global_seen.add(result.chunk_id)
            topic_used += len(content)
        response.append({"topic": topic, "chunks": chunks})
    return {"results": response}


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


async def _search_grouped(
    retrieval: RetrievalService,
    checks: list[BidSearchCheck] | list[TenderSearchCheck],
    query_filter: IndexFilter,
) -> list[tuple[str, list[SearchResult]]]:
    grouped: list[tuple[str, list[SearchResult]]] = []
    for check in checks:
        grouped.append(
            (
                check.topic,
                await _search_many(retrieval, check.queries, query_filter),
            )
        )
    return grouped


class SearchBidResponseTool(BaseTool):
    name: str = "search_bid_response"
    description: str = load_tool_description("search_bid_response")
    args_schema: ClassVar[type[BaseModel]] = SearchBidResponseInput
    model_config = ConfigDict(arbitrary_types_allowed=True)

    retrieval_service: RetrievalService
    settings: AppSettings
    event_repository: AuditEventRepository

    async def _arun(
        self,
        checks: list[BidSearchCheck],
        state: Annotated[dict, InjectedState],
    ) -> dict:
        bid_file = state["bid_files"][state["current_bid_index"]]
        grouped_results = await _search_grouped(
            self.retrieval_service,
            checks,
            IndexFilter(
                scope_id=f"bid-{state['task_id']}",
                project_id=state["project_id"],
                bidder_id=state["bidder_id"],
                task_id=state["task_id"],
                data_role="bid_document",
                file_ids=[bid_file.file_id],
            ),
        )
        response = _compact_grouped_results(
            grouped_results,
            per_chunk_chars=min(self.settings.retrieval.max_source_chars, 1800),
            total_chars=min(self.settings.retrieval.max_source_chars * 4, 16000),
        )
        self.event_repository.append(
            state["task_id"],
            actor="tool",
            event_type="tool_result",
            title="检索投标文件原文",
            detail={
                "tool": self.name,
                "file_id": bid_file.file_id,
                "topic_count": len(checks),
                "query_count": sum(len(check.queries) for check in checks),
                "result_count": sum(
                    len(item["chunks"]) for item in response["results"]
                ),
            },
        )
        return response

    def _run(self, **_: object) -> object:
        raise ToolException("search_bid_response 仅支持异步调用")


class SearchTenderSourceTool(BaseTool):
    name: str = "search_tender_source"
    description: str = load_tool_description("search_tender_source")
    args_schema: ClassVar[type[BaseModel]] = SearchTenderSourceInput
    model_config = ConfigDict(arbitrary_types_allowed=True)

    retrieval_service: RetrievalService
    settings: AppSettings
    event_repository: AuditEventRepository

    async def _arun(
        self,
        checks: list[TenderSearchCheck],
        state: Annotated[dict, InjectedState],
    ) -> dict:
        grouped_results = await _search_grouped(
            self.retrieval_service,
            checks,
            IndexFilter(
                scope_id=f"rules-{state['project_id']}",
                project_id=state["project_id"],
                data_role="rule_source",
            ),
        )
        response = _compact_grouped_results(
            grouped_results,
            per_chunk_chars=min(self.settings.retrieval.max_source_chars, 1800),
            total_chars=min(self.settings.retrieval.max_source_chars * 4, 16000),
        )
        self.event_repository.append(
            state["task_id"],
            actor="tool",
            event_type="tool_result",
            title="检索招标文件原文",
            detail={
                "tool": self.name,
                "topic_count": len(checks),
                "query_count": sum(len(check.queries) for check in checks),
                "result_count": sum(
                    len(item["chunks"]) for item in response["results"]
                ),
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
        checks: list[EvidenceReviewCheck],
        state: Annotated[dict, InjectedState],
    ) -> dict:
        rule_ids = {rule.rule_id for rule in state["rule_set"].rules}
        unknown = [check.rule_id for check in checks if check.rule_id not in rule_ids]
        if unknown:
            raise ToolException(f"资料核验引用了未知规则: {unknown}")
        results = []
        # 批次内部串行执行，避免一次工具调用同时压满模型服务。
        for check in checks:
            results.append(await self._review_one(check, state))
        return {"results": [result.model_dump(mode="json") for result in results]}

    async def _review_one(
        self,
        check: EvidenceReviewCheck,
        state: dict,
    ) -> EvidenceReviewResult:
        rule_id = check.rule_id
        evidence_type = check.evidence_type
        catalog_items = state["evidence_catalog"].get(str(evidence_type), [])
        available = [
            item for item in catalog_items if str(item.status) == "classified"
        ]
        if not available:
            has_manual_only = bool(catalog_items) and all(
                str(item.status) == "manual_review" for item in catalog_items
            )
            return EvidenceReviewResult(
                rule_id=rule_id,
                evidence_type=evidence_type,
                status="ocr_required" if has_manual_only else "unsupported",
                summary=(
                    "当前类别资料存在，但正文不可自动读取，需要人工核验"
                    if has_manual_only
                    else "当前资料目录中没有可自动核验的对应类别资料"
                ),
                evidence_chunk_ids=[],
            )

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
                evidence_type=evidence_type,
                status="unverifiable",
                summary="资料目录存在对应文件，但缺少可供子 Agent 使用的文件级摘要",
                evidence_chunk_ids=[],
            )
        review_task = EvidenceReviewTask(
            task_id=state["task_id"],
            project_id=state["project_id"],
            bidder_id=state["bidder_id"],
            rule_id=rule_id,
            evidence_type=evidence_type,
            verification_request=check.verification_request,
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
            evidence_type=evidence_type,
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
        return result

    def _run(self, **_: object) -> object:
        raise ToolException("request_evidence_review 仅支持异步调用")
