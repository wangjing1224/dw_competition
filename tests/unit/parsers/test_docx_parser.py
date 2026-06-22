"""DOCX 标题、段落和表格原始顺序测试。"""

from docx import Document

from app.models.documents import DocumentRef
from app.parsers.docx_parser import DocxParser


def test_docx_keeps_section_content_and_tables(tmp_path):
    path = tmp_path / "sample.docx"
    document = Document()
    document.add_heading("技术要求", level=1)
    document.add_paragraph("像素不低于 640x480")
    table = document.add_table(rows=2, cols=2)
    table.cell(0, 0).text = "参数"
    table.cell(0, 1).text = "要求"
    table.cell(1, 0).text = "精度"
    table.cell(1, 1).text = "±2℃"
    document.save(path)

    result = DocxParser().parse(
        DocumentRef(
            file_id="file-docx",
            file_name=path.name,
            source_path=path.name,
            data_role="rule_source",
            project_id="project-1",
        ),
        path,
    )

    assert result.parse_status == "parsed"
    assert result.sections[0].title == "技术要求"
    assert "640x480" in result.sections[0].content
    assert "±2℃" in result.sections[0].tables[0]
