"""主子 Agent 最后一轮必须禁用工具并强制输出。"""

from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage

from app.agents.bid_agent import bid_agent_node
from app.agents.evidence_subagent import evidence_agent_node, route_evidence_agent
from app.graph.audit_state import build_initial_audit_state
from app.models.documents import BidDocumentSummary, DocumentRef, SourceRef
from app.models.evidence import EvidenceFileSummary, EvidenceReviewTask
from app.models.rules import RuleItem, RuleSet
from app.models.tasks import AgentAuditDecision
from app.services.audit_task_service import AuditTaskContext
from app.storage.repositories import AuditEventRepository


class NoopEvents(AuditEventRepository):
    def append(self, *args, **kwargs):
        return None


class FinalOnlyModel:
    def __init__(self, response: str):
        self.response = response
        self.bound = False
        self.messages = None

    def bind_tools(self, _tools):
        self.bound = True
        raise AssertionError("最后一轮不应绑定工具")

    async def ainvoke(self, messages):
        self.messages = messages
        return AIMessage(content=self.response)


def _bid_state():
    bid_file = DocumentRef(
        file_id="bid-file",
        file_name="投标.docx",
        source_path="投标.docx",
        data_role="bid_document",
        parse_status="parsed",
        project_id="project-1",
        bidder_id="bidder-1",
        task_id="audit-1",
    )
    rule = RuleItem(
        rule_id="rule-1",
        category="technical",
        requirement="探测器分辨率不低于640x480",
        priority="critical",
        verification="bid_only",
        source=SourceRef(
            file_id="rule-file",
            file_name="规则.docx",
            source_path="规则.docx",
        ),
    )
    state = build_initial_audit_state(
        AuditTaskContext(
            task_id="audit-1",
            project_id="project-1",
            bidder_id="bidder-1",
            rule_set=RuleSet(project_id="project-1", rules=[rule]),
            bid_files=[bid_file],
            bid_summaries={
                "bid-file": BidDocumentSummary(
                    file_id="bid-file",
                    summary="红外热成像仪投标文件",
                    match_status="matched",
                    match_reason="项目一致",
                )
            },
        )
    )
    state["react_step_count"] = 7
    return state


@pytest.mark.asyncio
async def test_bid_agent_final_step_does_not_bind_tools():
    decision = AgentAuditDecision(
        summary="完成审核",
        categories=[
            {
                "category": "technical",
                "summary": "技术规则已审核",
                "rules": [
                    {
                        "rule_id": "rule-1",
                        "result": "manual_review",
                        "summary": {"reason": "现有信息不足"},
                    }
                ],
            }
        ],
    )
    model = FinalOnlyModel(decision.model_dump_json())

    updated = await bid_agent_node(
        _bid_state(),
        model=model,
        tools=[SimpleNamespace()],
        max_steps=8,
        event_repository=NoopEvents(),
    )

    assert updated["react_step_count"] == 8
    assert model.bound is False
    assert "第8轮" in model.messages[-1].content
    assert "禁止继续调用任何工具" in model.messages[-1].content


@pytest.mark.asyncio
async def test_evidence_agent_final_step_does_not_bind_tools():
    model = FinalOnlyModel(
        '{"status":"unverifiable","summary":"现有资料不足",'
        '"evidence_chunk_ids":[]}'
    )
    state = {
        "messages": [],
        "task_id": "audit-1",
        "project_id": "project-1",
        "bidder_id": "bidder-1",
        "review_task": EvidenceReviewTask(
            task_id="audit-1",
            project_id="project-1",
            bidder_id="bidder-1",
            rule_id="rule-1",
            evidence_type="contract",
            verification_request="核验合同业绩是否满足要求",
            files=[
                EvidenceFileSummary(
                    file_id="file-1",
                    evidence_summary="红外热成像仪供货合同",
                )
            ],
        ),
        "review_decision": None,
        "errors": [],
        "step_count": 2,
    }

    updated = await evidence_agent_node(
        state,
        model=model,
        tools=[SimpleNamespace()],
        max_steps=3,
        event_repository=NoopEvents(),
    )

    assert updated["step_count"] == 3
    assert model.bound is False
    assert "第3轮" in model.messages[-1].content
    assert "禁止继续调用任何工具" in model.messages[-1].content


def test_evidence_tool_calls_must_be_closed_before_finalize():
    state = {
        "messages": [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "call-1",
                        "name": "search_evidence",
                        "args": {"checks": []},
                    }
                ],
            )
        ],
        "step_count": 3,
    }

    assert route_evidence_agent(state) == "tools"


def test_evidence_invalid_text_finalizes_to_fallback():
    state = {
        "messages": [AIMessage(content="不是合法 JSON")],
        "step_count": 3,
    }

    assert route_evidence_agent(state) == "finalize"
