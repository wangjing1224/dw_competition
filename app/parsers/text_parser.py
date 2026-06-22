"""解析 TXT 和 Markdown，识别编码并保留简单标题层级。"""

import hashlib
import re
from pathlib import Path

from charset_normalizer import from_bytes

from app.models.documents import DocumentRef, ParsedDocument, ParsedSection


def _section_id(file_id: str, index: int, title: str) -> str:
    digest = hashlib.sha1(f"{file_id}:{index}:{title}".encode()).hexdigest()[:20]
    return f"section-{digest}"


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
                        section_id=_section_id(
                            document.file_id, start_index, title
                        ),
                        title=title,
                        content=body,
                        source_block_index=start_index,
                    )
                )
            content = []

        for index, line in enumerate(text.splitlines()):
            heading = re.match(r"^\s{0,3}#{1,6}\s+(.+?)\s*$", line)
            if heading:
                flush()
                title = heading.group(1).strip()
                start_index = index
            else:
                content.append(line.rstrip())
        flush()
        status = "parsed" if sections else "manual_review"
        return ParsedDocument(
            document=document.model_copy(update={"parse_status": status}),
            parse_status=status,
            sections=sections,
            warnings=[] if sections else ["文本文件没有可读取内容"],
        )
