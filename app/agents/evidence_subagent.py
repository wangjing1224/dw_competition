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
    """构造资料核验子 Agent 的消息。

    子 Agent 不直接看到完整规则库，也不负责判断投标文件是否合规。
    主 Agent 通过工具参数传入一个明确的 verification_request 和 evidence_type；
    子 Agent 只围绕这个资料类别做原文检索和核验结论。
    """

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


def build_force_finalize_message(current_step: int, max_steps: int) -> HumanMessage:
    """子 Agent 最后一轮禁用检索工具并强制形成核验结论。"""

    return HumanMessage(
        content=(
            f"当前是第{current_step}轮，总轮次上限为{max_steps}轮，"
            "你已经达到本次资料核验的循环上限。禁止继续调用任何工具。"
            "请立即根据核验要求、资料摘要和此前检索返回的原文片段形成最终结论。"
            "证据不足时使用unverifiable；图片不可读时使用ocr_required。"
            "只引用实际返回的evidence_chunk_ids，"
            "只返回符合EvidenceReviewDecision Schema的JSON。"
        )
    )


async def evidence_agent_node(
    state: dict[str, Any],
    *,
    model: BaseChatModel,
    tools: list[BaseTool],
    max_steps: int,
    event_repository: AuditEventRepository,
) -> dict[str, Any]:
    """资料核验子图的 Agent 节点。

    和主 Agent 一样，最大轮次控制放在节点内部、模型调用之前。
    最后一轮不绑定检索工具，避免模型在循环上限处继续发 tool_calls。
    """

    task = state["review_task"]
    current_step = state["step_count"] + 1
    is_final_step = current_step >= max_steps
    event_repository.append(
        state["task_id"],
        actor="evidence_agent",
        event_type="agent_turn_started",
        title="资料核验子 Agent 分析资料",
        detail={
            "step": current_step,
            "max_steps": max_steps,
            "force_finalize": is_final_step,
            "rule_id": task.rule_id,
            "evidence_type": str(task.evidence_type),
        },
    )
    messages = build_evidence_messages(state)
    if is_final_step:
        messages.append(build_force_finalize_message(current_step, max_steps))
        response = await model.ainvoke(messages)
    else:
        response = await model.bind_tools(tools).ainvoke(messages)
    return {"messages": [response], "step_count": current_step}


def can_parse_review_result(message: BaseMessage) -> bool:
    if not isinstance(message, AIMessage) or not isinstance(message.content, str):
        return False
    try:
        validate_structured_content(message.content, EvidenceReviewDecision)
        return True
    except Exception:
        return False


def route_evidence_agent(state: dict[str, Any]) -> str:
    """根据子 Agent 已经返回的消息选择下一步。

    这里不判断 max_steps。轮次预算属于“调用前策略”，已经在
    evidence_agent_node 中处理。

    如果 AIMessage 带 tool_calls，下一步必须先执行 tools，让 LangGraph 生成
    对应 ToolMessage。否则后续再次调用 OpenAI 兼容接口时，会因为 tool_calls
    没有闭合而 400。
    """

    last = state["messages"][-1]
    if isinstance(last, AIMessage) and last.tool_calls:
        return "tools"
    if can_parse_review_result(last):
        return "finalize"
    return "finalize"


def finalize_evidence_review(
    state: dict[str, Any],
    *,
    event_repository: AuditEventRepository,
) -> dict[str, Any]:
    """把子 Agent 的轻量 JSON 转成工具返回结果。

    这里会做一个关键兜底：模型只能引用本次子图工具实际返回过的
    evidence_chunk_ids。模型凭空编造的 chunk_id 会被过滤掉；如果支持性结论
    过滤后没有任何真实片段，则降级为 unverifiable。
    """

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
                groups = value.get("results", [])
                if not groups and "chunks" in value:
                    groups = [{"chunks": value.get("chunks", [])}]
                for group in groups:
                    if not isinstance(group, dict):
                        continue
                    allowed_chunk_ids.update(
                        str(chunk["chunk_id"])
                        for chunk in group.get("chunks", [])
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
