"""资料核验 Agent 模型节点、路由和结构化结果收口。"""

import json
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langchain_core.tools import BaseTool

from app.llm.structured_output import validate_structured_content
from app.models.common import generate_id
from app.models.evidence import EvidenceReviewResult
from app.prompts.loader import load_prompt


def build_evidence_messages(state: dict[str, Any]) -> list[BaseMessage]:
    claim = state["claim"]
    instruction = (
        f"当前 Claim：\n{claim.model_dump_json(indent=2)}\n\n"
        "必要时调用工具。形成结论后仅返回 EvidenceReviewResult JSON：\n"
        f"{json.dumps(EvidenceReviewResult.model_json_schema(), ensure_ascii=False)}"
    )
    return [
        SystemMessage(content=load_prompt("evidence_subagent")),
        HumanMessage(content=instruction),
        *state["messages"],
    ]


async def evidence_agent_node(
    state: dict[str, Any],
    *,
    model: BaseChatModel,
    tools: list[BaseTool],
) -> dict[str, Any]:
    response = await model.bind_tools(tools).ainvoke(build_evidence_messages(state))
    return {"messages": [response], "step_count": state["step_count"] + 1}


def can_parse_review_result(message: BaseMessage) -> bool:
    if not isinstance(message, AIMessage) or not isinstance(message.content, str):
        return False
    try:
        validate_structured_content(message.content, EvidenceReviewResult)
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


def finalize_evidence_review(state: dict[str, Any]) -> dict[str, Any]:
    claim = state["claim"]
    last = state["messages"][-1] if state["messages"] else None
    if last is not None and can_parse_review_result(last):
        result = validate_structured_content(last.content, EvidenceReviewResult)
    else:
        result = EvidenceReviewResult(
            evidence_result_id=generate_id("evidence-result"),
            claim_id=claim.claim_id,
            task_id=claim.task_id,
            project_id=claim.project_id,
            bidder_id=claim.bidder_id,
            bid_file_id=claim.bid_file_id,
            rule_id=claim.rule_id,
            status="unverifiable",
            evidence_found=[],
            missing_evidence=[],
            consistency_issues=[],
            manual_review_reason="资料核验达到步数上限或未返回合法结构化结果",
            summary="自动资料核验未能形成可靠结论，需人工复核",
        )
    expected = (
        claim.claim_id,
        claim.task_id,
        claim.project_id,
        claim.bidder_id,
        claim.bid_file_id,
        claim.rule_id,
    )
    actual = (
        result.claim_id,
        result.task_id,
        result.project_id,
        result.bidder_id,
        result.bid_file_id,
        result.rule_id,
    )
    if actual != expected:
        raise ValueError("EvidenceReviewResult 归属与 Claim 不一致")
    return {"review_result": result}
