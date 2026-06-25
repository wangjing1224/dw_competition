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


class SearchEvidenceInput(BaseModel):
    """资料子 Agent 原文检索工具的模型可见输入。"""

    model_config = ConfigDict(extra="forbid")
    queries: list[str] = Field(
        min_length=1,
        max_length=5,
        description=(
            "围绕主Agent给出的verification_request，在当前evidence_type资料中执行的"
            "1至5个互不重复查询。每个query只核验一个具体事实，并优先从资料摘要中提取"
            "主体名称、证书或合同名称、产品型号、编号、日期、金额及技术参数等高辨识度"
            "关键词。不要使用“核验是否符合”等宽泛表述；同一事实未召回有效原文后，"
            "不要仅改写同义词再次检索。"
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


def _compact(results: list[SearchResult], settings: AppSettings) -> dict:
    chunks: list[dict[str, str]] = []
    seen: set[str] = set()
    used = 0
    total_limit = min(settings.retrieval.max_source_chars * 2, 8000)
    for result in sorted(results, key=lambda item: item.score, reverse=True):
        if result.chunk_id in seen or len(chunks) >= 5:
            continue
        remaining = total_limit - used
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
        seen.add(result.chunk_id)
        used += len(content)
    return {"chunks": chunks}


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
        queries: list[str],
        file_ids: list[str],
        state: Annotated[dict, InjectedState],
    ) -> dict:
        task = state["review_task"]
        allowed_file_ids = {item.file_id for item in task.files}
        if not allowed_file_ids:
            return {"chunks": []}
        selected = set(file_ids) if file_ids else allowed_file_ids
        if not selected <= allowed_file_ids:
            raise ToolException("资料检索请求包含未授权文件")

        results: list[SearchResult] = []
        for query in dict.fromkeys(item.strip() for item in queries if item.strip()):
            results.extend(
                await self.retrieval_service.search(
                    query,
                    IndexFilter(
                        scope_id=f"evidence-{task.project_id}-{task.bidder_id}",
                        project_id=task.project_id,
                        bidder_id=task.bidder_id,
                        data_role="evidence_material",
                        evidence_types=[task.evidence_type],
                        file_ids=sorted(selected),
                    ),
                    candidate_top_k=20,
                    final_top_k=8,
                )
            )
        response = _compact(results, self.settings)
        self.event_repository.append(
            task.task_id,
            actor="tool",
            event_type="tool_result",
            title="检索投标方资料原文",
            detail={
                "tool": self.name,
                "rule_id": task.rule_id,
                "evidence_type": str(task.evidence_type),
                "query_count": len(queries),
                "result_count": len(response["chunks"]),
            },
        )
        return response

    def _run(self, **_: object) -> object:
        raise ToolException("search_evidence 仅支持异步调用")
