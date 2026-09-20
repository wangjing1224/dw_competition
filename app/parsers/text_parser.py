"""解析 TXT 和 Markdown，识别编码并保留简单标题层级。"""

import hashlib
import re
from pathlib import Path

from charset_normalizer import from_bytes

from app.models.documents import DocumentRef, ParsedBlock, ParsedDocument, ParsedSection
from app.parsers.common import finish_document


def _section_id(file_id: str, index: int, title: str) -> str:
    digest = hashlib.sha1(f"{file_id}:{index}:{title}".encode()).hexdigest()[:20]
    return f"section-{digest}"


def text_blocks(body: str, start_index: int) -> list[ParsedBlock]:
    """识别 Markdown 表格/HTML 表格，按结构分隔而不是按字符截断。"""
    lines = body.splitlines()
    blocks, paragraph = [], []

    def flush():
        if paragraph:
            blocks.append(
                ParsedBlock(
                    kind="text",
                    content="\n".join(paragraph).strip(),
                    source_block_index=start_index + len(blocks),
                )
            )
            paragraph.clear()

    index = 0
    while index < len(lines):
        line = lines[index]
        if re.match(r"\s*(```|~~~)", line):
            flush()
            marker = line.strip()[:3]
            code = [line]
            index += 1
            while index < len(lines):
                code.append(lines[index])
                index += 1
                if code[-1].strip().startswith(marker):
                    break
            paragraph.extend(code)
            flush()
            continue
        markdown_table = (
            index + 1 < len(lines)
            and "|" in line
            and re.fullmatch(
                r"\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|?\s*", lines[index + 1]
            )
        )
        html_table = line.lstrip().startswith("<table")
        if markdown_table or html_table:
            flush()
            table = [line]
            index += 1
            while index < len(lines):
                if html_table and "</table>" in table[-1]:
                    break
                if not html_table and (
                    not lines[index].strip() or "|" not in lines[index]
                ):
                    break
                table.append(lines[index])
                index += 1
            blocks.append(
                ParsedBlock(
                    kind="table",
                    content="\n".join(table),
                    source_block_index=start_index + len(blocks),
                )
            )
            continue
        if line.strip():
            paragraph.append(line)
        else:
            flush()
        index += 1
    flush()
    return blocks


class TextParser:
    def supports(self, suffix: str) -> bool:
        return suffix.lower() in {".txt", ".md"}

    def parse(self, document: DocumentRef, stored_path: Path) -> ParsedDocument:
        matched = from_bytes(stored_path.read_bytes()).best()
        if matched is None:
            return ParsedDocument(
                document=document.model_copy(update={"parse_status": "manual_review"}),
                parse_status="manual_review",
                warnings=["无法可靠识别文本编码"],
            )

        text = str(matched).replace("\r\n", "\n").replace("\r", "\n")
        sections: list[ParsedSection] = []
        title = "文档开始"
        start_index = 0
        content: list[str] = []

        def flush() -> None:
            nonlocal content
            body = "\n".join(content).strip()
            if body:
                sections.append(
                    ParsedSection(
                        section_id=_section_id(document.file_id, start_index, title),
                        title=title,
                        blocks=text_blocks(body, start_index),
                        source_block_index=start_index,
                    )
                )
            content = []

        fence = None
        for index, line in enumerate(text.splitlines()):
            marker = re.match(r"^\s*(```|~~~)", line)
            if marker:
                fence = None if fence == marker.group(1) else marker.group(1)
            heading = (
                re.match(r"^\s{0,3}#{1,6}\s+(.+?)\s*$", line) if not fence else None
            )
            if heading:
                flush()
                title = heading.group(1).strip()
                start_index = index
            else:
                content.append(line.rstrip())
        flush()
        return finish_document(
            document,
            stored_path,
            sections,
            [] if sections else ["文本文件没有可读取内容"],
        )
