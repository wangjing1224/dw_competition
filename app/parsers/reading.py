"""解析结构的有序阅读单元；供不同 Agent 的输入适配器共用。"""

from dataclasses import dataclass

from app.models.documents import ParsedDocument
from app.parsers.render import render_block


@dataclass(frozen=True)
class DocumentUnit:
    section_index: int
    block_index: int


def build_document_units(parsed: ParsedDocument) -> list[DocumentUnit]:
    """顺序位置绑定调用方的文件/解析快照；不生成另一套 chunk。"""
    if parsed.schema_version != 2:
        raise ValueError(f"{parsed.document.file_id} 缺少有序解析结构，请重新解析")
    units = [
        DocumentUnit(section_index, block_index)
        for section_index, section in enumerate(parsed.sections)
        for block_index, _ in enumerate(section.blocks)
    ]
    if not units or not any(
        block.content.strip() or block.images
        for section in parsed.sections
        for block in section.blocks
    ):
        raise ValueError(f"{parsed.document.file_id} 没有可阅读的正文、表格或图片")
    return units


def render_document_range(parsed, units, start, end) -> str:
    """表格、图片及已有转写整体渲染；从章节中间开始也重新显示标题。"""
    parts = []
    previous_section = None
    for unit in units[start:end]:
        section = parsed.sections[unit.section_index]
        if previous_section != unit.section_index:
            parts.append(f"## {section.title}")
            previous_section = unit.section_index
        block = section.blocks[unit.block_index]
        pages = sorted({source.page_number for source in block.page_sources})
        if pages:
            parts.append("页码：" + "、".join(map(str, pages)))
        parts.append(render_block(block))
    if parsed.warnings:
        parts.append("## 解析限制\n" + "\n".join(parsed.warnings))
    return "\n\n".join(parts)


def build_document_context(parsed: ParsedDocument) -> str:
    """完整投影仍可复用，Agent 装载范围由控制器选择。"""
    units = build_document_units(parsed)
    return render_document_range(parsed, units, 0, len(units))
