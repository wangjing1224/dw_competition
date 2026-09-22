"""同一份有序结构派生 Markdown，旧 JSON 不伪造阅读顺序。"""

from app.models.documents import ParsedBlock, ParsedDocument


def render_block(block: ParsedBlock) -> str:
    parts = [block.content] if block.content else []
    if block.content_origin == "vision_assisted":
        parts.insert(0, "[视觉辅助恢复，必要时回查原图]")
    for image in block.images:
        if image.purpose == "reference":
            parts.append(f"[表格原图：{image.description or '回查区域'}]({image.path})")
            continue
        if image.path not in block.content:
            parts.append(f"![图片]({image.path})")
        if image.description:
            parts.append(f"图片说明：{image.description}")
        if image.transcription:
            parts.append(f"[图片解读，非原生文本]\n{image.transcription}")
        if image.purpose == "content" and image.read_status != "readable":
            parts.append(f"图片读取状态：{image.read_status}")
        if image.warning:
            parts.append(f"读取限制：{image.warning}")
    parts.extend(f"读取限制：{warning}" for warning in block.warnings)
    return "\n\n".join(parts)


def render_document(parsed: ParsedDocument) -> str:
    parts = []
    if parsed.schema_version == 1:
        parts.append(
            "[旧解析结果：正文与表格原始交错顺序、图片资产可能缺失，必要时重新解析]"
        )
    for section in parsed.sections:
        if parsed.schema_version == 2 and not section.blocks:
            continue
        parts.append(f"## {section.title}")
        if parsed.schema_version == 2:
            parts.extend(render_block(block) for block in section.blocks)
        else:
            parts.extend([section.content, *section.tables])
    if parsed.warnings:
        parts.append("## 解析限制\n" + "\n".join(f"- {w}" for w in parsed.warnings))
    return "\n\n".join(p for p in parts if p).strip() + "\n"
