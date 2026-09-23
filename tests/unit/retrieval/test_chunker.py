"""稳定分段和 overlap 测试。"""

from app.models.documents import (
    DocumentRef,
    PageSource,
    ParsedBlock,
    ParsedDocument,
    ParsedImage,
    ParsedSection,
)
from app.retrieval.chunker import chunk_parsed_document, split_with_overlap


def test_split_with_overlap_has_shared_tokens():
    chunks = split_with_overlap("甲乙丙丁戊己", max_tokens=4, overlap_tokens=2)

    assert chunks == ["甲乙丙丁", "丙丁戊己"]


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
    assert first[0].chunk_id == "chunk-1-1-1-1"


def test_v2_table_is_whole_and_carries_page_image_and_version():
    image = ParsedImage(
        path="file-1/images/1.png",
        mime_type="image/png",
        width=100,
        height=100,
        purpose="reference",
    )
    table = "<table>" + "<tr><td>技术指标640×480</td></tr>" * 200 + "</table>"
    parsed = ParsedDocument(
        schema_version=2,
        markdown_path="file-1/versions/a/document.md",
        document=DocumentRef(
            file_id="file-1",
            project_id="project-1",
            data_role="rule_source",
            file_name="技术.pdf",
            source_path="技术.pdf",
        ),
        parse_status="parsed",
        sections=[
            ParsedSection(
                section_id="s",
                title="技术表",
                blocks=[
                    ParsedBlock(
                        kind="table",
                        content=table,
                        source_block_index=0,
                        images=[image],
                        page_sources=[PageSource(page_number=3, image_path=image.path)],
                    )
                ],
            )
        ],
    )
    chunks = chunk_parsed_document(parsed, max_tokens=10, overlap_tokens=1)
    assert len(chunks) == 1 and chunks[0].content == table
    assert chunks[0].page_numbers == [3] and chunks[0].image_paths == [image.path]
    assert chunks[0].parse_version == parsed.markdown_path
    parsed.markdown_path = "file-1/versions/b/document.md"
    rebuilt = chunk_parsed_document(parsed, max_tokens=10, overlap_tokens=1)
    assert rebuilt[0].chunk_id != chunks[0].chunk_id
