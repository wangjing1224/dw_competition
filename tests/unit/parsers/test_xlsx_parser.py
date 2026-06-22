"""XLSX 多 Sheet 解析测试。"""

from openpyxl import Workbook

from app.models.documents import DocumentRef
from app.parsers.xlsx_parser import XlsxParser


def test_each_sheet_becomes_a_section(tmp_path):
    path = tmp_path / "sample.xlsx"
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "包1"
    sheet.append(["物资", "数量"])
    sheet.append(["红外热成像仪", 2])
    workbook.create_sheet("包2").append(["物资", "数量"])
    workbook.save(path)

    result = XlsxParser().parse(
        DocumentRef(
            file_id="file-xlsx",
            file_name=path.name,
            source_path=path.name,
            data_role="rule_source",
            project_id="project-1",
        ),
        path,
    )

    assert result.parse_status == "parsed"
    assert [section.title for section in result.sections] == [
        "Sheet: 包1",
        "Sheet: 包2",
    ]
    assert "红外热成像仪" in result.sections[0].tables[0]
