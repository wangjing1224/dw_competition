"""校验查询身份字段并返回符合范围的索引行号。"""

from app.models.common import DataRole
from app.retrieval.models import IndexChunk, IndexFilter


def filter_chunk_indices(
    chunks: list[IndexChunk], query_filter: IndexFilter
) -> list[int]:
    if query_filter.data_role == DataRole.EVIDENCE_MATERIAL:
        if not query_filter.bidder_id:
            raise ValueError("资料检索必须提供 bidder_id")
    elif query_filter.data_role == DataRole.BID_DOCUMENT:
        if not query_filter.bidder_id or not query_filter.task_id:
            raise ValueError("投标文件检索必须提供 bidder_id 和 task_id")

    matched: list[int] = []
    for index, chunk in enumerate(chunks):
        if chunk.project_id != query_filter.project_id:
            continue
        if chunk.data_role != query_filter.data_role:
            continue
        if (
            query_filter.bidder_id is not None
            and chunk.bidder_id != query_filter.bidder_id
        ):
            continue
        if query_filter.task_id is not None and chunk.task_id != query_filter.task_id:
            continue
        if query_filter.file_ids and chunk.file_id not in query_filter.file_ids:
            continue
        if (
            query_filter.evidence_types
            and chunk.evidence_type not in query_filter.evidence_types
        ):
            continue
        matched.append(index)
    return matched
