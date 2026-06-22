"""文本编码和 Markdown 章节切分测试。"""

from app.models.documents import DocumentRef
from app.parsers.text_parser import TextParser


def test_markdown_headings_become_sections(tmp_path):
    path = tmp_path / "sample.md"
    path.write_text("# 商务要求\n投标有效期 90 天\n# 技术要求\n精度 ±2℃", "utf-8")

    result = TextParser().parse(
        DocumentRef(
            file_id="file-text",
            file_name=path.name,
            source_path=path.name,
            data_role="rule_source",
            project_id="project-1",
        ),
        path,
    )

    assert [section.title for section in result.sections] == [
        "商务要求",
        "技术要求",
    ]
    assert "90 天" in result.sections[0].content
