"""PDF 原生解析与受控页面/表格截图；此层不调用远程模型。"""

from html import escape
from pathlib import Path

import pymupdf

from app.core.config import get_settings
from app.models.documents import DocumentRef, PageSource, ParsedBlock, ParsedSection
from app.parsers.assets import AssetStore
from app.parsers.common import assets_for, finish_document
from app.parsers.docx_parser import stable_section_id


def render_region(page, bbox, assets, *, purpose="reference"):
    dpi = get_settings().parsing.pdf_render_dpi
    rect = pymupdf.Rect(bbox)
    if rect.width * rect.height * (dpi / 72) ** 2 > assets.max_pixels:
        raise ValueError("页面渲染超过像素上限")
    data = page.get_pixmap(clip=rect, dpi=dpi, alpha=False).tobytes("png")
    return assets.save_image(
        data, purpose=purpose, description=f"第 {page.number + 1} 页区域"
    )


def native_table_html(page, table):
    """使用单元格几何重建 rowspan/colspan，不把缺失单元格猜成空值。"""
    cells = sorted({tuple(round(float(v), 2) for v in c) for c in table.cells if c})
    xs = sorted({c[0] for c in cells} | {c[2] for c in cells})
    ys = sorted({c[1] for c in cells} | {c[3] for c in cells})
    if not cells or len(xs) > 200 or len(ys) > 2000:
        raise ValueError("表格单元格几何无效或过大")
    occupied, rows = set(), []
    for y_index, y in enumerate(ys[:-1]):
        row = []
        for c in [cell for cell in cells if cell[1] == y]:
            x0, x1, y1 = xs.index(c[0]), xs.index(c[2]), ys.index(c[3])
            for r in range(y_index, y1):
                for col in range(x0, x1):
                    if (r, col) in occupied:
                        raise ValueError("表格单元格重叠")
                    occupied.add((r, col))
            text = escape(page.get_textbox(pymupdf.Rect(c)).strip()).replace(
                "\n", "<br>"
            )
            row.append(f'<td rowspan="{y1 - y_index}" colspan="{x1 - x0}">{text}</td>')
        rows.append("<tr>" + "".join(row) + "</tr>")
    if len(occupied) != (len(xs) - 1) * (len(ys) - 1):
        raise ValueError("表格有未覆盖的单元格区域")
    return "<table>\n" + "\n".join(rows) + "\n</table>"


class PdfParser:
    def supports(self, suffix: str) -> bool:
        return suffix.lower() == ".pdf"

    def parse(
        self,
        document: DocumentRef,
        stored_path: Path,
        *,
        assets: AssetStore | None = None,
    ):
        assets = assets or assets_for(document)
        sections, warnings = [], []
        with pymupdf.open(stored_path) as pdf:
            if pdf.needs_pass:
                return finish_document(
                    document, stored_path, [], ["PDF 已加密，未提供解密能力"]
                )
            if pdf.page_count > get_settings().parsing.max_pdf_pages:
                return finish_document(
                    document,
                    stored_path,
                    [],
                    ["PDF 页数超过解析上限，未截断冒充完整文档"],
                )
            for page in pdf:
                try:
                    # 只修改内存页面；保持视觉外观并统一到显示方向坐标，不改原文件。
                    if page.rotation:
                        page.remove_rotation()
                    blocks, page_warnings = self._page(page, assets)
                    warnings.extend(page_warnings)
                    sections.append(
                        ParsedSection(
                            section_id=stable_section_id(
                                document.file_id, page.number, "PDF"
                            ),
                            title=f"第 {page.number + 1} 页",
                            blocks=blocks,
                        )
                    )
                except Exception as exc:
                    warnings.append(
                        f"第 {page.number + 1} 页解析失败：{type(exc).__name__}，需回查原文件"
                    )
        return finish_document(document, stored_path, sections, warnings)

    def _page(self, page, assets):
        blocks, warnings, tables = [], [], []
        try:
            detected = page.find_tables().tables
        except Exception:
            detected = []
            warnings.append(f"第 {page.number + 1} 页表格检测失败，保留页面回查图")
        for table in detected:
            bbox = tuple(table.bbox)
            try:
                html = native_table_html(page, table)
                image = render_region(page, bbox, assets)
                block = ParsedBlock(
                    kind="table",
                    content=html,
                    images=[image],
                    source_block_index=0,
                    page_sources=[
                        PageSource(
                            page_number=page.number + 1,
                            bbox=bbox,
                            image_path=image.path,
                        )
                    ],
                )
                tables.append((pymupdf.Rect(bbox), block))
                blocks.append(block)
            except Exception:
                warnings.append(
                    f"第 {page.number + 1} 页表格结构未完整恢复，保留原文与页面图"
                )
        raw = page.get_text("dict", sort=True)["blocks"]
        for item in raw:
            bbox = tuple(item["bbox"])
            rect = pymupdf.Rect(bbox)
            table_block = next((b for r, b in tables if r.contains(rect)), None)
            if item["type"] == 0:
                # 按行排除表格内文字，避免整段包围表格时误删表外正文。
                lines = []
                for line in item.get("lines", []):
                    if not any(
                        r.contains(pymupdf.Rect(line["bbox"])) for r, _ in tables
                    ):
                        lines.append("".join(span["text"] for span in line["spans"]))
                text = "\n".join(lines).strip()
                if text:
                    blocks.append(
                        ParsedBlock(
                            kind="text",
                            content=text,
                            source_block_index=0,
                            page_sources=[
                                PageSource(page_number=page.number + 1, bbox=bbox)
                            ],
                        )
                    )
            elif item["type"] == 1:
                try:
                    # 渲染区域保留旋转、透明蒙版和叠字，不只取压缩资源裸图。
                    image = render_region(page, bbox, assets, purpose="content")
                    if table_block:
                        table_block.images.append(image)
                    else:
                        blocks.append(
                            ParsedBlock(
                                kind="image",
                                images=[image],
                                source_block_index=0,
                                page_sources=[
                                    PageSource(
                                        page_number=page.number + 1,
                                        bbox=bbox,
                                        image_path=image.path,
                                    )
                                ],
                            )
                        )
                except Exception as exc:
                    warnings.append(
                        f"第 {page.number + 1} 页图片提取失败：{type(exc).__name__}"
                    )
        if warnings or not blocks:
            # 无文字/图像块可能是路径描出的扫描内容，不能当作空白页直接跳过。
            try:
                image = render_region(page, tuple(page.rect), assets, purpose="content")
                blocks.append(
                    ParsedBlock(
                        kind="image",
                        images=[image],
                        source_block_index=0,
                        page_sources=[
                            PageSource(
                                page_number=page.number + 1,
                                bbox=tuple(page.rect),
                                image_path=image.path,
                            )
                        ],
                        warnings=["页面结构待视觉读取；原生提取文字仅供参考"]
                        if warnings
                        else [],
                    )
                )
            except Exception as exc:
                warnings.append(
                    f"第 {page.number + 1} 页回查图未保存：{type(exc).__name__}；已提取文本仍保留"
                )
        blocks.sort(
            key=lambda b: (b.page_sources[0].bbox[1], b.page_sources[0].bbox[0])
        )
        for index, block in enumerate(blocks):
            block.source_block_index = index
        return blocks, warnings
