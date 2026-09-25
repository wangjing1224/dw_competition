"""资料核验只返回正文，不解析结果 JSON，不制造无法核验兜底。"""

from langchain_core.messages import HumanMessage, SystemMessage

from app.audit.session import audit_agent_node as evidence_agent_node
from app.prompts.loader import render_prompt

__all__ = ["build_evidence_messages", "evidence_agent_node"]


def build_evidence_messages(check, selected, relations, details, handoff=""):
    return [
        SystemMessage(content=render_prompt("evidence_subagent")),
        HumanMessage(
            content=render_prompt(
                "evidence_subagent_input",
                verification_request=check.verification_request,
                file_ids="、".join(check.file_ids),
                relations=relations,
                evidence_files=details,
                continuation=handoff or "这是独立核验任务。",
            )
        ),
    ]
