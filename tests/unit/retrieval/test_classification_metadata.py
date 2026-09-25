"""资料分类完成后的最新元数据必须进入索引片段。"""

from app.models.documents import DocumentRef, ParsedDocument, ParsedSection
from app.retrieval.chunker import chunk_parsed_document


def test_classified_document_metadata_is_inherited_by_chunk():
    document = DocumentRef(
        file_id="evidence-1",
        file_name="合同.docx",
        source_path="合同.docx",
        data_role="evidence_material",
        parse_status="parsed",
        project_id=None,
        bidder_id="bidder-1",
        evidence_type="contract",
        classification_status="classified",
    )
    parsed = ParsedDocument(
        document=document,
        parse_status="parsed",
        sections=[
            ParsedSection(
                section_id="section-1",
                title="合同标的",
                content="红外热成像仪 20 台",
            )
        ],
    )

    chunks = chunk_parsed_document(parsed, max_tokens=100, overlap_tokens=10)

    assert chunks[0].evidence_type == "contract"
    assert chunks[0].classification_status == "classified"
