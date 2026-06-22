"""资料核验子图 State 和初始状态构造。"""

from typing import Annotated, TypedDict

from langchain_core.messages import BaseMessage
from langgraph.graph.message import add_messages

from app.models.evidence import Claim, EvidenceReviewResult
from app.models.tasks import TaskError


class EvidenceState(TypedDict):
    messages: Annotated[list[BaseMessage], add_messages]
    task_id: str
    project_id: str
    bidder_id: str
    claim: Claim
    review_result: EvidenceReviewResult | None
    errors: list[TaskError]
    step_count: int


def build_initial_evidence_state(claim: Claim) -> EvidenceState:
    return {
        "messages": [],
        "task_id": claim.task_id,
        "project_id": claim.project_id,
        "bidder_id": claim.bidder_id,
        "claim": claim,
        "review_result": None,
        "errors": [],
        "step_count": 0,
    }
