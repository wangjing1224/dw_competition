"""资料核验子 Agent：最多三轮检索并返回轻量结论。"""

from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.tools import BaseTool

from app.llm.structured_output import extract_json_value, validate_structured_content
from app.models.evidence import EvidenceReviewDecision
from app.prompts.loader import render_prompt
from app.storage.repositories import AuditEventRepository


def build_evidence_messages(state: dict[str, Any]) -> list[BaseMessage]:
    task = state["review_task"]
    return [
        SystemMessage(
            content=render_prompt(
                "evidence_subagent",
                evidence_review_schema=(
                    EvidenceReviewDecision.model_json_schema()
                ),
            )
        ),
        HumanMessage(
            content=render_prompt(
                "evidence_subagent_input",
                verification_request=task.verification_request,
                evidence_type=task.evidence_type,
                evidence_files=[
                    item.model_dump(mode="json") for item in task.files
                ],
            )
        ),
        *state["messages"],
    ]


async def evidence_agent_node(
    state: dict[str, Any],
    *,
    model: BaseChatModel,
    tools: list[BaseTool],
    event_repository: AuditEventRepository,
) -> dict[str, Any]:
    task = state["review_task"]
    event_repository.append(
        state["task_id"],
        actor="evidence_agent",
        event_type="agent_turn_started",
        title="资料核验子 Agent 分析资料",
        detail={
            "step": state["step_count"] + 1,
            "rule_id": task.rule_id,
            "evidence_type": str(task.evidence_type),
        },
    )
    response = await model.bind_tools(tools).ainvoke(build_evidence_messages(state))
    return {"messages": [response], "step_count": state["step_count"] + 1}


def can_parse_review_result(message: BaseMessage) -> bool:
    if not isinstance(message, AIMessage) or not isinstance(message.content, str):
        return False
    try:
        validate_structured_content(message.content, EvidenceReviewDecision)
        return True
    except Exception:
        return False


def route_evidence_agent(state: dict[str, Any], max_steps: int) -> str:
    last = state["messages"][-1]
    if isinstance(last, AIMessage) and last.tool_calls:
        return "limit_reached" if state["step_count"] >= max_steps else "tools"
    if can_parse_review_result(last):
        return "finalize"
    return "limit_reached" if state["step_count"] >= max_steps else "agent"


def finalize_evidence_review(
    state: dict[str, Any],
    *,
    event_repository: AuditEventRepository,
) -> dict[str, Any]:
    last = state["messages"][-1] if state["messages"] else None
    if last is not None and can_parse_review_result(last):
        decision = validate_structured_content(
            last.content,
            EvidenceReviewDecision,
        )
        allowed_chunk_ids: set[str] = set()
        for message in state["messages"]:
            if not isinstance(message, ToolMessage) or not isinstance(
                message.content, str
            ):
                continue
            try:
                value = extract_json_value(message.content)
            except Exception:
                continue
            if isinstance(value, dict):
                allowed_chunk_ids.update(
                    str(chunk["chunk_id"])
                    for chunk in value.get("chunks", [])
                    if isinstance(chunk, dict) and chunk.get("chunk_id")
                )
        decision = decision.model_copy(
            update={
                "evidence_chunk_ids": [
                    chunk_id
                    for chunk_id in dict.fromkeys(
                        decision.evidence_chunk_ids
                    )
                    if chunk_id in allowed_chunk_ids
                ]
            }
        )
        if (
            str(decision.status)
            in {"supported", "partially_supported", "contradicted"}
            and not decision.evidence_chunk_ids
        ):
            decision = EvidenceReviewDecision(
                status="unverifiable",
                summary="子 Agent 未引用实际检索片段，自动核验结论无有效来源",
                evidence_chunk_ids=[],
            )
    else:
        decision = EvidenceReviewDecision(
            status="unverifiable",
            summary="资料核验达到步数上限或未返回合法结果，需要人工复核",
            evidence_chunk_ids=[],
        )
    task = state["review_task"]
    event_repository.append(
        state["task_id"],
        actor="evidence_agent",
        event_type="evidence_decision",
        title="资料核验子 Agent 形成结论",
        detail={
            "rule_id": task.rule_id,
            "status": str(decision.status),
            "evidence_count": len(decision.evidence_chunk_ids),
        },
    )
    return {"review_decision": decision}
