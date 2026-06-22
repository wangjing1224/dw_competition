"""将 XLSX 的每个 Sheet 解析为独立表格章节。"""

import hashlib
from pathlib import Path
from typing import Any

from openpyxl import load_workbook

from app.models.documents import DocumentRef, ParsedDocument, ParsedSection


def _section_id(file_id: str, sheet_name: str) -> str:
    digest = hashlib.sha1(f"{file_id}:{sheet_name}".encode()).hexdigest()[:20]
    return f"section-{digest}"


def _cell_text(value: Any) -> str:
    return "" if value is None else str(value).strip().replace("|", r"\|")


def _rows_to_markdown(rows: list[list[str]]) -> str:
    rows = [row for row in rows if any(cell for cell in row)]
    if not rows:
        return ""
    width = max(len(row) for row in rows)
    rows = [row + [""] * (width - len(row)) for row in rows]
    return "\n".join(
        [
            f"| {' | '.join(rows[0])} |",
            f"| {' | '.join(['---'] * width)} |",
            *(f"| {' | '.join(row)} |" for row in rows[1:]),
        ]
    )


class XlsxParser:
    def supports(self, suffix: str) -> bool:
        return suffix.lower() == ".xlsx"

    def parse(self, document: DocumentRef, stored_path: Path) -> ParsedDocument:
        workbook = load_workbook(stored_path, data_only=True, read_only=True)
        sections: list[ParsedSection] = []
        for sheet in workbook.worksheets:
            rows = [
                [_cell_text(cell) for cell in row]
                for row in sheet.iter_rows(values_only=True)
            ]
            markdown = _rows_to_markdown(rows)
            if markdown:
                sections.append(
                    ParsedSection(
                        section_id=_section_id(document.file_id, sheet.title),
                        title=f"Sheet: {sheet.title}",
                        tables=[markdown],
                    )
                )
        status = "parsed" if sections else "manual_review"
        return ParsedDocument(
            document=document.model_copy(update={"parse_status": status}),
            parse_status=status,
            sections=sections,
            warnings=[] if sections else ["工作簿中没有可读取内容"],
        )
