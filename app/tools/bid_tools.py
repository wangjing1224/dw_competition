"""主 Agent 委派工具：文件级选择，一层关系展开，逐 check 独立子会话。"""

from typing import Annotated
from uuid import uuid4

from langchain_core.tools import InjectedToolCallId, StructuredTool
from langgraph.errors import NodeCancelledError
from pydantic import Field

from app.agents.evidence_subagent import build_evidence_messages
from app.audit.context import evidence_view, scope_for
from app.audit.session import run_session
from app.audit.sources import SourceLedger
from app.models.common import StrictModel
from app.prompts.loader import load_tool_description
from app.tools.common_tools import CommonTool


class EvidenceCheck(StrictModel):
    rule_id: str = Field(
        min_length=1, description="引用当前规则 ID，仅由系统关联，不进入子 Agent。"
    )
    verification_request: str = Field(
        min_length=1,
        description="一个明确核验事项，写清待核声明、主体/对象、编号/数值及判断标准。",
    )
    file_ids: list[str] = Field(
        min_length=1,
        max_length=30,
        description="当前资料目录中的非空起始文件 ID 列表；系统补充一层直接关系文件。",
    )


class EvidenceReviewInput(StrictModel):
    checks: list[EvidenceCheck] = Field(
        min_length=1,
        max_length=10,
        description="核验请求列表，每项含 rule_id、verification_request、file_ids；同一 rule_id 可重复，结果按顺序返回。",
    )
    tool_call_id: Annotated[str, InjectedToolCallId]


def build_evidence_review_tool(
    *,
    context,
    settings,
    model,
    retrieval,
    ledger,
    limiter,
    trace_context,
    records,
    events=None,
):
    cache = {}
    known_rules = {rule.rule_id for rule in context.rule_set.rules}

    async def request_evidence_review(
        checks, tool_call_id: Annotated[str, InjectedToolCallId]
    ):
        outputs = []
        # 先验证整批身份，避免后项越权时前项已耗费模型调用。
        if any(check.rule_id not in known_rules for check in checks):
            raise ValueError("委派引用未知规则")
        for check in checks:
            evidence_view(context.evidence_library, check.file_ids)
        for number, check in enumerate(checks, 1):
            key = (
                (trace_context.get("metadata") or {}).get("batch_index"),
                tool_call_id,
                number,
            )
            if key in cache and cache[key][0] != check.model_dump():
                raise ValueError("同一工具调用 ID 不能用于不同的委派，请使用新的调用")
            if key not in cache:
                selected, relations, details = evidence_view(
                    context.evidence_library, check.file_ids
                )
                parsed = {key: context.parsed_documents[key] for key in selected}
                child_ledger = SourceLedger(parsed)
                tools = CommonTool(
                    parsed_documents=parsed,
                    settings=settings,
                    retrieval=retrieval,
                    file_filters={
                        key: scope_for(value.document) for key, value in parsed.items()
                    },
                    default_file_ids=selected,
                    on_chunk_read=child_ledger.chunk_read,
                    on_image_read=child_ledger.image_read,
                    strict_errors=True,
                ).tools()
                child_trace = {
                    **trace_context,
                    "metadata": {
                        **(trace_context.get("metadata") or {}),
                        "rule_id": check.rule_id,
                        "check_index": number,
                        "child_session": uuid4().hex,
                        "verification_request": check.verification_request,
                        "target_file_ids": selected,
                    },
                }
                stats = {}
                try:
                    content = await run_session(
                        model=model,
                        tools=tools,
                        settings=settings.evidence_chat_model,
                        max_steps=settings.audit.evidence_agent_max_steps,
                        role="evidence_subagent",
                        limiter=limiter,
                        trace_context=child_trace,
                        build_messages=lambda handoff, check=check, selected=selected, relations=relations, details=details: (
                            build_evidence_messages(
                                check, selected, relations, details, handoff
                            )
                        ),
                        stats=stats,
                        events=events,
                    )
                except NodeCancelledError:
                    raise
                except Exception as exc:
                    # 子会话执行失败由父工具边界反馈；不是资料不足或核验结论。
                    raise RuntimeError(
                        f"资料核验执行失败：{type(exc).__name__}: {exc}"
                    ) from exc
                record = {
                    **check.model_dump(),
                    "authorized_file_ids": selected,
                    "content": content,
                    "sources": {
                        k: v.model_dump(mode="json")
                        for k, v in child_ledger.sources.items()
                    },
                    "stats": stats,
                }
                records.append(record)
                ledger.sources.update(child_ledger.sources)
                cache[key] = (check.model_dump(), content)
            outputs.append(
                f"## 资料核验结果{number}｜{check.rule_id}\n\n"
                f"- 核验事项：{check.verification_request}\n- 起始资料：{'、'.join(check.file_ids)}\n\n{cache[key][1]}"
            )
        return "\n\n".join(outputs)

    return StructuredTool.from_function(
        coroutine=request_evidence_review,
        name="request_evidence_review",
        args_schema=EvidenceReviewInput,
        description=load_tool_description("request_evidence_review"),
    )
