"""将 XLSX 的每个 Sheet 解析为独立表格章节。"""

import hashlib
from io import StringIO
from html import escape
from pathlib import Path

from openpyxl import load_workbook

from app.core.config import get_settings
from app.models.documents import DocumentRef, ParsedBlock, ParsedDocument, ParsedSection
from app.parsers.assets import AssetStore
from app.parsers.common import assets_for, finish_document


def _section_id(file_id: str, sheet_name: str) -> str:
    digest = hashlib.sha1(f"{file_id}:{sheet_name}".encode()).hexdigest()[:20]
    return f"section-{digest}"


class XlsxParser:
    def supports(self, suffix: str) -> bool:
        return suffix.lower() == ".xlsx"

    def parse(
        self,
        document: DocumentRef,
        stored_path: Path,
        *,
        assets: AssetStore | None = None,
    ) -> ParsedDocument:
        # 普通模式才能读取合并区域与嵌入图片；公式保留原表达式，不能把无缓存值当空白。
        workbook = load_workbook(stored_path, data_only=False)
        assets = assets or assets_for(document)
        sections: list[ParsedSection] = []
        warnings = []
        try:
            for sheet in workbook.worksheets:
                blocks = []
                if (
                    sheet.max_row * sheet.max_column
                    > get_settings().parsing.max_sheet_cells
                ):
                    warnings.append(
                        f"工作表 {sheet.title} 超过单元格处理上限，未截断冒充完整表"
                    )
                    continue
                merges, covered = {}, set()
                for region in sheet.merged_cells.ranges:
                    merges[(region.min_row, region.min_col)] = (
                        region.max_row - region.min_row + 1,
                        region.max_col - region.min_col + 1,
                    )
                    covered.update(
                        (r, c)
                        for r in range(region.min_row, region.max_row + 1)
                        for c in range(region.min_col, region.max_col + 1)
                        if (r, c) != (region.min_row, region.min_col)
                    )
                rows = []
                for row in sheet.iter_rows():
                    cells = []
                    for cell in row:
                        if (cell.row, cell.column) in covered:
                            continue
                        rs, cs = merges.get((cell.row, cell.column), (1, 1))
                        value = "" if cell.value is None else str(cell.value)
                        if cell.data_type == "f":
                            value = "[公式，未计算] " + value
                        cells.append(
                            f'<td rowspan="{rs}" colspan="{cs}">{escape(value)}</td>'
                        )
                    rows.append("<tr>" + "".join(cells) + "</tr>")
                if any(
                    cell.value is not None for row in sheet.iter_rows() for cell in row
                ):
                    blocks.append(
                        ParsedBlock(
                            kind="table",
                            content="<table>\n" + "\n".join(rows) + "\n</table>",
                            source_block_index=0,
                        )
                    )
                for index, embedded in enumerate(sheet._images, start=1):
                    try:
                        anchor = getattr(embedded.anchor, "_from", None)
                        location = (
                            f"工作表 {sheet.title}，行 {anchor.row + 1} 列 {anchor.col + 1}"
                            if anchor
                            else f"工作表 {sheet.title}"
                        )
                        image = assets.save_image(
                            embedded._data(), description=location
                        )
                        blocks.append(
                            ParsedBlock(
                                kind="image", images=[image], source_block_index=index
                            )
                        )
                    except Exception as exc:
                        warnings.append(
                            f"工作表 {sheet.title} 图片未提取：{type(exc).__name__}"
                        )
                if not blocks:
                    continue
                sections.append(
                    ParsedSection(
                        section_id=_section_id(document.file_id, sheet.title),
                        title=f"Sheet: {sheet.title}",
                        blocks=blocks,
                    )
                )
        finally:
            workbook.close()
        return finish_document(document, stored_path, sections, warnings)


class XlsParser:
    """读取嵌入的 BIFF XLS 保存值及合并区域，不通过改扩展名伪装成 XLSX。"""

    def parse(self, document, stored_path, *, assets=None):
        import xlrd

        book = xlrd.open_workbook(
            file_contents=stored_path.read_bytes(), formatting_info=True,
            logfile=StringIO(), on_demand=True,
        )
        sections, warnings = [], []
        try:
            for sheet in book.sheets():
                if sheet.nrows * sheet.ncols > get_settings().parsing.max_sheet_cells:
                    warnings.append(f"工作表 {sheet.name} 超过单元格处理上限，未截断冒充完整表")
                    continue
                merges, covered = {}, set()
                for r0, r1, c0, c1 in sheet.merged_cells:
                    merges[r0, c0] = (r1 - r0, c1 - c0)
                    covered.update(
                        (r, c) for r in range(r0, r1) for c in range(c0, c1)
                        if (r, c) != (r0, c0)
                    )
                rows = []
                for r in range(sheet.nrows):
                    cells = []
                    for c in range(sheet.ncols):
                        if (r, c) in covered:
                            continue
                        cell = sheet.cell(r, c)
                        value = cell.value
                        if cell.ctype == xlrd.XL_CELL_DATE:
                            value = xlrd.xldate_as_datetime(value, book.datemode).isoformat(sep=" ")
                        elif cell.ctype == xlrd.XL_CELL_BOOLEAN:
                            value = "TRUE" if value else "FALSE"
                        elif cell.ctype == xlrd.XL_CELL_ERROR:
                            value = xlrd.error_text_from_code.get(value, "[单元格错误]")
                        elif cell.ctype == xlrd.XL_CELL_NUMBER and value.is_integer():
                            value = int(value)
                        rs, cs = merges.get((r, c), (1, 1))
                        content = escape(str(value)).replace("\n", "<br>")
                        cells.append(f'<td rowspan="{rs}" colspan="{cs}">{content}</td>')
                    rows.append("<tr>" + "".join(cells) + "</tr>")
                if rows:
                    sections.append(ParsedSection(
                        section_id=_section_id(document.file_id, sheet.name),
                        title=f"Sheet: {sheet.name}",
                        blocks=[ParsedBlock(
                            kind="table", content="<table>\n" + "\n".join(rows) + "\n</table>",
                            source_block_index=0,
                        )],
                    ))
        finally:
            book.release_resources()
        # xlrd 不读取图形/子附件，也不计算公式，不能把这些能力当作已完成。
        warnings.append("旧版 XLS 仅读取已保存单元格值及合并区域；公式未重算，图形与子附件未展开")
        return finish_document(document, stored_path, sections, warnings)
