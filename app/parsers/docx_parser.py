"""按原始顺序解析 DOCX 段落、标题、表格和图片状态。"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Iterator

from docx import Document
from docx.document import Document as DocxDocument
from docx.table import Table
from docx.text.paragraph import Paragraph

from app.models.documents import DocumentRef, ParsedDocument, ParsedSection


def stable_section_id(file_id: str, block_index: int, title: str) -> str:
    value = f"{file_id}:{block_index}:{title}".encode()
    return f"section-{hashlib.sha1(value).hexdigest()[:20]}"


def _clean_text(value: str) -> str:
    return re.sub(r"[ \t\u3000]+", " ", value).strip()


def _iter_blocks(document: DocxDocument) -> Iterator[Paragraph | Table]:
    for child in document.element.body.iterchildren():
        if child.tag.endswith("}p"):
            yield Paragraph(child, document)
        elif child.tag.endswith("}tbl"):
            yield Table(child, document)


def _heading_level(paragraph: Paragraph) -> int | None:
    style_name = getattr(paragraph.style, "name", "") or ""
    matched = re.search(r"(?:Heading|标题)\s*(\d+)", style_name, re.I)
    return int(matched.group(1)) if matched else None


def _image_count(paragraph: Paragraph) -> int:
    return len(paragraph._element.xpath(".//a:blip"))


def _escape_cell(value: str) -> str:
    return _clean_text(value).replace("|", r"\|").replace("\n", "<br>")


def table_to_markdown(table: Table) -> str:
    rows = [[_escape_cell(cell.text) for cell in row.cells] for row in table.rows]
    if not rows:
        return ""
    width = max(len(row) for row in rows)
    normalized = [row + [""] * (width - len(row)) for row in rows]
    header = normalized[0]
    lines = [
        f"| {' | '.join(header)} |",
        f"| {' | '.join(['---'] * width)} |",
    ]
    lines.extend(f"| {' | '.join(row)} |" for row in normalized[1:])
    return "\n".join(lines)


class DocxParser:
    def supports(self, suffix: str) -> bool:
        return suffix.lower() == ".docx"

    def parse(self, document: DocumentRef, stored_path: Path) -> ParsedDocument:
        doc = Document(stored_path)
        sections: list[ParsedSection] = []
        heading_stack: list[str] = []
        current_title = "文档开始"
        current_index = 0
        content_parts: list[str] = []
        tables: list[str] = []
        image_count = 0

        def flush() -> None:
            nonlocal content_parts, tables, image_count
            if not any(content_parts) and not any(tables) and image_count == 0:
                return
            sections.append(
                ParsedSection(
                    section_id=stable_section_id(
                        document.file_id, current_index, current_title
                    ),
                    title=current_title,
                    content="\n".join(part for part in content_parts if part),
                    tables=[table for table in tables if table],
                    image_count=image_count,
                    source_block_index=current_index,
                )
            )
            content_parts = []
            tables = []
            image_count = 0

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
                    current_title = " > ".join(
                        item for item in heading_stack if item
                    )
                    current_index = block_index
                elif text:
                    content_parts.append(text)
                image_count += _image_count(block)
            else:
                markdown = table_to_markdown(block)
                if markdown:
                    tables.append(markdown)

        flush()
        has_text = any(section.content or section.tables for section in sections)
        total_images = sum(section.image_count for section in sections)
        if has_text:
            status = "partial" if total_images and not any(
                section.content for section in sections
            ) else "parsed"
        elif total_images:
            status = "ocr_required"
        else:
            status = "manual_review"
        return ParsedDocument(
            document=document.model_copy(update={"parse_status": status}),
            parse_status=status,
            sections=sections,
            warnings=(
                ["文档包含图片，图片文字未执行 OCR"] if total_images else []
            ),
        )
