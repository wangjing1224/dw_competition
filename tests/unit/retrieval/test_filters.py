"""项目、投标方和任务强制隔离测试。"""

from app.retrieval.filters import filter_chunk_indices
from app.retrieval.models import IndexChunk, IndexFilter


def chunk(bidder_id):
    return IndexChunk(
        chunk_id=f"chunk-{bidder_id}",
        file_id=f"file-{bidder_id}",
        project_id=None,
        bidder_id=bidder_id,
        data_role="evidence_material",
        file_name="合同.docx",
        source_path="合同.docx",
        section="合同标的",
        content="红外热成像仪",
        parse_status="parsed",
        evidence_type="contract",
    )


def test_evidence_filter_never_crosses_bidder():
    indices = filter_chunk_indices(
        [chunk("bidder-a"), chunk("bidder-b")],
        IndexFilter(
            scope_id="evidence-project-1-bidder-a",
            project_id=None,
            bidder_id="bidder-a",
            data_role="evidence_material",
        ),
    )

    assert indices == [0]
