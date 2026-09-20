"""按原始顺序解析 DOCX 段落、标题、表格和图片状态。"""

from __future__ import annotations

import hashlib
import re
from html import escape
from pathlib import Path
from typing import Iterator

from docx import Document
from docx.document import Document as DocxDocument
from docx.oxml.ns import qn
from docx.table import Table
from docx.text.paragraph import Paragraph

from app.models.documents import DocumentRef, ParsedBlock, ParsedDocument, ParsedSection
from app.parsers.assets import AssetStore
from app.parsers.common import assets_for, finish_document


def stable_section_id(file_id: str, block_index: int, title: str) -> str:
    value = f"{file_id}:{block_index}:{title}".encode()
    return f"section-{hashlib.sha1(value).hexdigest()[:20]}"


def _clean_text(value: str) -> str:
    return re.sub(r"[ \t\u3000]+", " ", value).strip()


def _iter_blocks(document: DocxDocument) -> Iterator[Paragraph | Table]:
    def walk(element):
        for child in element.iterchildren():
            if child.tag == qn("w:p"):
                yield Paragraph(child, document)
            elif child.tag == qn("w:tbl"):
                yield Table(child, document)
            elif child.tag in {qn("w:sdt"), qn("w:sdtContent"), qn("w:customXml")}:
                yield from walk(child)

    yield from walk(document.element.body)


def _heading_level(paragraph: Paragraph) -> int | None:
    style_name = getattr(paragraph.style, "name", "") or ""
    matched = re.search(r"(?:Heading|标题)\s*(\d+)", style_name, re.I)
    return int(matched.group(1)) if matched else None


class DocxParser:
    def supports(self, suffix: str) -> bool:
        return suffix.lower() == ".docx"

    def parse(
        self,
        document: DocumentRef,
        stored_path: Path,
        *,
        assets: AssetStore | None = None,
    ) -> ParsedDocument:
        doc = Document(stored_path)
        assets = assets or assets_for(document)
        warnings: list[str] = []
        sections: list[ParsedSection] = []
        heading_stack: list[str] = []
        current_title = "文档开始"
        current_index = 0
        blocks: list[ParsedBlock] = []

        def flush() -> None:
            nonlocal blocks
            if not blocks:
                return
            sections.append(
                ParsedSection(
                    section_id=stable_section_id(
                        document.file_id, current_index, current_title
                    ),
                    title=current_title,
                    blocks=blocks,
                    source_block_index=current_index,
                )
            )
            blocks = []

        for block_index, block in enumerate(_iter_blocks(doc)):
            if isinstance(block, Paragraph):
                text = _clean_text(block.text)
                level = _heading_level(block)
                if level is not None and text:
                    flush()
                    del heading_stack[level - 1 :]
                    while len(heading_stack) < level - 1:
                        heading_stack.append("")
                    heading_stack.append(text)
                    current_title = " > ".join(item for item in heading_stack if item)
                    current_index = block_index
                parts = paragraph_parts(block._element, doc.part, assets, warnings)
                for kind, value in parts:
                    if kind == "text" and level is None and value.strip():
                        blocks.append(
                            ParsedBlock(
                                kind="text",
                                content=value.strip(),
                                source_block_index=block_index,
                            )
                        )
                    elif kind == "image":
                        blocks.append(
                            ParsedBlock(
                                kind="image",
                                images=[value],
                                source_block_index=block_index,
                            )
                        )
            else:
                html, images = table_to_html(block._tbl, doc.part, assets, warnings)
                blocks.append(
                    ParsedBlock(
                        kind="table",
                        content=html,
                        images=images,
                        source_block_index=block_index,
                    )
                )

        flush()
        return finish_document(document, stored_path, sections, warnings)


def paragraph_parts(element, part, assets: AssetStore, warnings: list[str]):
    """读取 run 内的实际次序，涵盖超链接内文字、DrawingML 与旧式 VML 图片。"""
    parts, text = [], []
    for item in element.iter():
        local = item.tag.rsplit("}", 1)[-1]
        if item.tag == qn("w:t"):
            text.append(item.text or "")
        elif item.tag in {qn("w:br"), qn("w:cr")}:
            text.append("\n")
        elif item.tag == qn("w:tab"):
            text.append("\t")
        elif local in {"blip", "imagedata"}:
            if text:
                parts.append(("text", "".join(text)))
                text = []
            relationship = item.get(qn("r:embed")) or item.get(qn("r:id"))
            try:
                # 外部链接不自动下载；未支持的 WMF 等格式明确留痕，不改后缀冒充。
                if not relationship:
                    raise ValueError("图片不是已嵌入的资源")
                image = assets.save_image(part.related_parts[relationship].blob)
                parts.append(("image", image))
            except Exception as exc:
                warnings.append(f"嵌入图片未提取：{type(exc).__name__}，需回查原文件")
                parts.append(("text", "[图片未能提取，需回查原文件]"))
        elif local == "anchor":
            warnings.append("存在浮动对象：按 XML 锚点排序，视觉阅读顺序需回查原文件")
    if text:
        parts.append(("text", "".join(text)))
    return parts


def table_to_html(tbl, part, assets: AssetStore, warnings: list[str]):
    """按 XML 实际单元格恢复横/纵合并；不把首行擅自认作表头。"""
    rows, images, previous = [], [], {}
    for tr in tbl.findall(qn("w:tr")):
        row, active = [], {}
        before = tr.find("./" + qn("w:trPr") + "/" + qn("w:gridBefore"))
        column = int(before.get(qn("w:val"), "0")) if before is not None else 0
        if column:
            row.append({"body": "", "cols": column, "rows": 1})
        for tc in tr.findall(qn("w:tc")):
            props = tc.find(qn("w:tcPr"))
            span = props.find(qn("w:gridSpan")) if props is not None else None
            width = int(span.get(qn("w:val"), "1")) if span is not None else 1
            vertical = props.find(qn("w:vMerge")) if props is not None else None
            parts = []
            for child in tc:
                if child.tag == qn("w:p"):
                    paragraph = []
                    for kind, value in paragraph_parts(child, part, assets, warnings):
                        if kind == "text":
                            paragraph.append(escape(value).replace("\n", "<br>"))
                        else:
                            images.append(value)
                            paragraph.append(f'<img src="{value.path}" alt="图片">')
                    parts.append("".join(paragraph))
                elif child.tag == qn("w:tbl"):
                    nested, nested_images = table_to_html(child, part, assets, warnings)
                    parts.append(nested)
                    images.extend(nested_images)
            body = "<br>".join(p for p in parts if p)
            continuation = (
                vertical is not None and vertical.get(qn("w:val")) != "restart"
            )
            parent = previous.get(column) if continuation else None
            if parent is not None and parent["cols"] == width:
                parent["rows"] += 1
                if body:
                    parent["body"] += "<br>" + body
                cell = parent
            else:
                if continuation:
                    warnings.append("纵向合并单元格起点不明，保留本行内容待回查")
                cell = {"body": body, "cols": width, "rows": 1}
                row.append(cell)
            if vertical is not None:
                active[column] = cell
            column += width
        rows.append(row)
        previous = active
    html = (
        "<table>\n"
        + "\n".join(
            "<tr>"
            + "".join(
                f'<td colspan="{c["cols"]}" rowspan="{c["rows"]}">{c["body"]}</td>'
                for c in row
            )
            + "</tr>"
            for row in rows
        )
        + "\n</table>"
    )
    return html, images
