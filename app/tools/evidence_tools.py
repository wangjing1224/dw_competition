"""资料核验子 Agent 的资料原文检索工具。"""

from typing import Annotated, ClassVar

from langchain_core.tools import BaseTool, ToolException
from langgraph.prebuilt import InjectedState
from pydantic import BaseModel, ConfigDict, Field

from app.core.config import AppSettings
from app.prompts.loader import load_tool_description
from app.retrieval.models import IndexFilter, SearchResult
from app.retrieval.service import RetrievalService
from app.storage.repositories import AuditEventRepository


class EvidenceSearchCheck(BaseModel):
    """当前资料类别内的单个待核验事实。"""

    model_config = ConfigDict(extra="forbid")
    topic: str = Field(
        min_length=2,
        max_length=100,
        description=(
            "简短描述一个待核验事实，例如“证书有效期”或“合同金额”。"
            "不要把多个无关事实合并为一个topic。"
        ),
    )
    queries: list[str] = Field(
        min_length=1,
        max_length=5,
        description=(
            "围绕当前topic提供1至5个互补查询。优先包含主体名称、证书或合同名称、"
            "产品型号、编号、日期、金额及技术参数等高辨识度信息。"
        ),
    )


class SearchEvidenceInput(BaseModel):
    """资料子 Agent 原文检索工具的模型可见输入。"""

    model_config = ConfigDict(extra="forbid")
    checks: list[EvidenceSearchCheck] = Field(
        min_length=1,
        max_length=8,
        description=(
            "当前资料类别中需要核验的1至8个独立事实。每个check使用topic说明一个"
            "核验问题，并提供1至5个从资料摘要提取的互补query。"
        ),
    )
    file_ids: list[str] = Field(
        default_factory=list,
        max_length=10,
        description=(
            "可选的资料文件范围，最多10个。资料摘要已经指向具体文件时，填写任务中提供的"
            "对应file_id以缩小检索范围；无法确定具体文件时留空，系统将检索当前"
            "evidence_type下全部可用文件。不得填写或猜测任务未提供的file_id。"
        ),
    )
    state: Annotated[dict, InjectedState]


def _compact_grouped(
    grouped_results: list[tuple[str, list[SearchResult]]],
    settings: AppSettings,
) -> dict[str, list[dict[str, object]]]:
    results: list[dict[str, object]] = []
    global_seen: set[str] = set()
    total_limit = min(settings.retrieval.max_source_chars * 3, 12000)
    topic_budget = max(600, total_limit // max(len(grouped_results), 1))
    for topic, search_results in grouped_results:
        chunks: list[dict[str, str]] = []
        local_seen: set[str] = set()
        used = 0
        ranked = sorted(
            search_results,
            key=lambda item: item.score,
            reverse=True,
        )
        candidates = [
            result for result in ranked if result.chunk_id not in global_seen
        ]
        if not candidates:
            candidates = ranked
        for result in candidates:
            if result.chunk_id in local_seen or len(chunks) >= 2:
                continue
            remaining = topic_budget - used
            if remaining <= 0:
                break
            content = result.content[: min(1800, remaining)]
            chunks.append(
                {
                    "chunk_id": result.chunk_id,
                    "file_id": result.source.file_id,
                    "section": result.source.section or "",
                    "content": content,
                }
            )
            local_seen.add(result.chunk_id)
            global_seen.add(result.chunk_id)
            used += len(content)
        results.append({"topic": topic, "chunks": chunks})
    return {"results": results}


class SearchEvidenceTool(BaseTool):
    name: str = "search_evidence"
    description: str = load_tool_description("search_evidence")
    args_schema: ClassVar[type[BaseModel]] = SearchEvidenceInput
    model_config = ConfigDict(arbitrary_types_allowed=True)

    retrieval_service: RetrievalService
    settings: AppSettings
    event_repository: AuditEventRepository

    async def _arun(
        self,
        checks: list[EvidenceSearchCheck],
        file_ids: list[str],
        state: Annotated[dict, InjectedState],
    ) -> dict:
        task = state["review_task"]
        allowed_file_ids = {item.file_id for item in task.files}
        if not allowed_file_ids:
            return {
                "results": [
                    {"topic": check.topic, "chunks": []} for check in checks
                ]
            }
        selected = set(file_ids) if file_ids else allowed_file_ids
        if not selected <= allowed_file_ids:
            raise ToolException("资料检索请求包含未授权文件")

        query_filter = IndexFilter(
            scope_id=f"evidence-{task.project_id}-{task.bidder_id}",
            project_id=task.project_id,
            bidder_id=task.bidder_id,
            data_role="evidence_material",
            evidence_types=[task.evidence_type],
            file_ids=sorted(selected),
        )
        grouped_results: list[tuple[str, list[SearchResult]]] = []
        for check in checks:
            topic_results: list[SearchResult] = []
            for query in dict.fromkeys(
                item.strip() for item in check.queries if item.strip()
            ):
                topic_results.extend(
                    await self.retrieval_service.search(
                        query,
                        query_filter,
                        candidate_top_k=20,
                        final_top_k=8,
                    )
                )
            grouped_results.append((check.topic, topic_results))
        response = _compact_grouped(grouped_results, self.settings)
        self.event_repository.append(
            task.task_id,
            actor="tool",
            event_type="tool_result",
            title="检索投标方资料原文",
            detail={
                "tool": self.name,
                "rule_id": task.rule_id,
                "evidence_type": str(task.evidence_type),
                "topic_count": len(checks),
                "query_count": sum(len(check.queries) for check in checks),
                "result_count": sum(
                    len(item["chunks"]) for item in response["results"]
                ),
            },
        )
        return response

    def _run(self, **_: object) -> object:
        raise ToolException("search_evidence 仅支持异步调用")
