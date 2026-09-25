"""核验使用同一短循环实现，结束直接返回正文，无 JSON finalize 节点。"""

from app.audit.session import build_session_graph


def build_evidence_graph(**kwargs):
    return build_session_graph(role="evidence_subagent", **kwargs)
