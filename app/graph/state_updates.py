"""校验轻量 Agent Decision，并确定性拼接 Finding 与 AuditResult。"""

from typing import Any

from langchain_core.messages import HumanMessage, ToolMessage
from langgraph.graph.message import REMOVE_ALL_MESSAGES, RemoveMessage

from app.graph.audit_state import AuditState, current_file
from app.graph.routing import can_parse_decision
from app.llm.structured_output import extract_json_value
from app.models.findings import Finding
from app.models.results import AuditResult, FileAuditResult, ProjectMatch
from app.models.rules import RuleReportItem, present_rule_for_model
from app.models.tasks import AgentAuditDecision
from app.storage.repositories import AuditEventRepository


CONCLUSION_ORDER = {
    "non_compliant": 0,
    "risk": 1,
    "manual_review": 2,
    "compliant": 3,
}


def summarize_conclusion(findings: list[Finding]) -> str:
    if not findings:
        return "manual_review"
    return min((str(item.result) for item in findings), key=CONCLUSION_ORDER.__getitem__)


def _fallback_findings(state: AuditState) -> list[Finding]:
    return [
        Finding(
            rule_id=rule.rule_id,
            result="manual_review",
            summary="主 Agent 达到步数上限或未返回合法审核结果，需要人工复核",
        )
        for rule in state["rule_set"].rules
    ]


def _materialize_findings(
    state: AuditState,
    decision: AgentAuditDecision,
) -> tuple[list[Finding], dict[str, str]]:
    rules = {rule.rule_id: rule for rule in state["rule_set"].rules}
    findings: list[Finding] = []
    category_summaries: dict[str, str] = {}
    allowed_chunks = _tool_chunk_ids(state)
    seen: set[str] = set()
    for category in decision.categories:
        category_key = str(category.category)
        if category_key in category_summaries:
            raise ValueError(f"分类 {category_key} 被重复输出")
        category_summaries[category_key] = category.summary
        for item in category.rules:
            rule = rules.get(item.rule_id)
            if rule is None:
                raise ValueError(f"主 Agent 返回未知规则: {item.rule_id}")
            if str(rule.category) != category_key:
                raise ValueError(f"规则 {item.rule_id} 被放入错误分类")
            if item.rule_id in seen:
                raise ValueError(f"规则 {item.rule_id} 被重复输出")
            seen.add(item.rule_id)
            findings.append(
                Finding(
                    rule_id=item.rule_id,
                    result=item.result,
                    summary=item.summary.reason,
                    tender_chunk_ids=list(
                        dict.fromkeys(
                            chunk_id
                            for chunk_id in item.summary.tender_chunk_ids
                            if chunk_id in allowed_chunks["tender"]
                        )
                    ),
                    bid_chunk_ids=list(
                        dict.fromkeys(
                            chunk_id
                            for chunk_id in item.summary.bid_chunk_ids
                            if chunk_id in allowed_chunks["bid"]
                        )
                    ),
                    evidence_chunk_ids=list(
                        dict.fromkeys(
                            chunk_id
                            for chunk_id in item.summary.evidence_chunk_ids
                            if chunk_id in allowed_chunks["evidence"]
                        )
                    ),
                )
            )
    if seen != set(rules):
        missing = sorted(set(rules) - seen)
        raise ValueError(f"主 Agent 未覆盖全部规则: {missing}")
    return findings, category_summaries


def _tool_chunk_ids(state: AuditState) -> dict[str, set[str]]:
    """最终结果只能引用当前消息历史中工具实际返回的片段。"""

    allowed = {"tender": set(), "bid": set(), "evidence": set()}
    tool_to_kind = {
        "search_tender_source": "tender",
        "search_bid_response": "bid",
    }
    for message in state["messages"]:
        if not isinstance(message, ToolMessage) or not isinstance(
            message.content, str
        ):
            continue
        try:
            value = extract_json_value(message.content)
        except Exception:
            continue
        kind = tool_to_kind.get(message.name or "")
        if kind and isinstance(value, dict):
            allowed[kind].update(
                str(chunk["chunk_id"])
                for chunk in value.get("chunks", [])
                if isinstance(chunk, dict) and chunk.get("chunk_id")
            )
        if message.name == "request_evidence_review" and isinstance(value, dict):
            allowed["evidence"].update(
                str(chunk_id)
                for chunk_id in value.get("evidence_chunk_ids", [])
            )
    return allowed


def prepare_length_retry_node(state: AuditState) -> dict[str, Any]:
    last = state["messages"][-1]
    messages: list[Any] = []
    if getattr(last, "id", None):
        messages.append(RemoveMessage(id=last.id))
    messages.append(
        HumanMessage(
            content=(
                "上一次最终结果因输出长度限制被截断。请基于已有上下文重新输出完整 JSON。"
                "不要重复规则正文和来源内容；总体总结不超过500字，分类总结不超过300字，"
                "每条规则 reason 不超过100字；完整覆盖全部规则；不要重新调用工具。"
                "只返回 JSON。"
            )
        )
    )
    return {"messages": messages, "length_retry_count": 1}


def prepare_format_retry_node(state: AuditState) -> dict[str, Any]:
    last = state["messages"][-1]
    messages: list[Any] = []
    if getattr(last, "id", None):
        messages.append(RemoveMessage(id=last.id))
    messages.append(
        HumanMessage(
            content=(
                "上一次最终输出不是合法的 AgentAuditDecision JSON。"
                "只修复 JSON 语法、字段层级、枚举和遗漏规则，不增加新事实，不调用工具。"
                "必须完整覆盖全部规则，只返回 JSON。"
            )
        )
    )
    return {"messages": messages, "format_retry_count": 1}


def apply_bid_agent_output_node(
    state: AuditState,
    *,
    event_repository: AuditEventRepository | None = None,
) -> dict[str, Any]:
    last = state["messages"][-1] if state["messages"] else None
    decision = (
        AgentAuditDecision.model_validate(extract_json_value(last.content))
        if last is not None and can_parse_decision(last)
        else None
    )
    if decision is None:
        findings = _fallback_findings(state)
        category_summaries = {}
        audit_summary = "自动审核未能形成合法结构化结果，当前文件需要人工复核"
    else:
        try:
            findings, category_summaries = _materialize_findings(state, decision)
            audit_summary = decision.summary
        except ValueError as exc:
            findings = _fallback_findings(state)
            category_summaries = {}
            audit_summary = f"自动审核结果覆盖校验失败，需要人工复核：{exc}"

    bid_file = current_file(state)
    summary = state["bid_summaries"][bid_file.file_id]
    file_result = FileAuditResult(
        file=bid_file,
        is_applicable=True,
        project_match=ProjectMatch(
            status="matched",
            reason=summary.match_reason,
        ),
        document_summary=summary,
        overall_conclusion=summarize_conclusion(findings),
        audit_summary=audit_summary,
        category_summaries=category_summaries,
        findings=findings,
        suggestions=(
            ["请人工复核报告中标记为人工复核的规则"]
            if any(str(item.result) == "manual_review" for item in findings)
            else []
        ),
    )
    if event_repository is not None:
        event_repository.append(
            state["task_id"],
            actor="main_agent",
            event_type="agent_decision",
            title="主 Agent 完成当前文件审核",
            detail={
                "file_id": bid_file.file_id,
                "finding_count": len(findings),
                "overall_conclusion": str(file_result.overall_conclusion),
            },
        )
    return {
        "messages": [RemoveMessage(id=REMOVE_ALL_MESSAGES)],
        "findings": [*state["findings"], *findings],
        "file_results": [*state["file_results"], file_result],
        "current_bid_index": state["current_bid_index"] + 1,
        "react_step_count": 0,
        "length_retry_count": 0,
        "format_retry_count": 0,
    }


def build_audit_result_node(state: AuditState) -> dict[str, AuditResult]:
    by_file = {item.file.file_id: item for item in state["file_results"]}
    return {
        "audit_result": AuditResult(
            task_id=state["task_id"],
            project_id=state["project_id"],
            bidder_id=state["bidder_id"],
            rules=[
                RuleReportItem.model_validate(
                    present_rule_for_model(rule).model_dump(mode="json")
                )
                for rule in state["rule_set"].rules
            ],
            evidence_classifications=state["evidence_classifications"],
            file_results=[
                by_file[file_id] for file_id in state["bid_summaries"].keys()
            ],
        )
    }
