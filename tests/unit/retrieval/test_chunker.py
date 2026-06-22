"""稳定分段和 overlap 测试。"""

from app.models.documents import DocumentRef, ParsedDocument, ParsedSection
from app.retrieval.chunker import chunk_parsed_document, split_with_overlap


def test_split_with_overlap_has_shared_tokens():
    chunks = split_with_overlap("甲乙丙丁戊己", max_tokens=4, overlap_tokens=2)

    assert chunks == ["甲 乙 丙 丁", "丙 丁 戊 己"]


def test_chunk_id_is_stable():
    parsed = ParsedDocument(
        document=DocumentRef(
            file_id="file-1",
            file_name="规则.txt",
            source_path="规则.txt",
            data_role="rule_source",
            parse_status="parsed",
            project_id="project-1",
        ),
        parse_status="parsed",
        sections=[
            ParsedSection(
                section_id="section-1",
                title="技术要求",
                content="像素不低于640x480",
            )
        ],
    )

    first = chunk_parsed_document(parsed, max_tokens=20, overlap_tokens=2)
    second = chunk_parsed_document(parsed, max_tokens=20, overlap_tokens=2)

    assert first[0].chunk_id == second[0].chunk_id
