"""资料核验子图 State。"""

from typing import Annotated, TypedDict

from langchain_core.messages import BaseMessage
from langgraph.graph.message import add_messages

from app.models.evidence import EvidenceReviewDecision, EvidenceReviewTask
from app.models.tasks import TaskError


class EvidenceState(TypedDict):
    messages: Annotated[list[BaseMessage], add_messages]
    task_id: str
    project_id: str
    bidder_id: str
    review_task: EvidenceReviewTask
    review_decision: EvidenceReviewDecision | None
    errors: list[TaskError]
    step_count: int
