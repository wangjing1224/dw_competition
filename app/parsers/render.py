"""同一份有序结构派生 Markdown，旧 JSON 不伪造阅读顺序。"""

import re

from app.models.documents import ParsedBlock, ParsedDocument


def _render_transcription(text: str) -> str:
    """图片内的 # 标题放在章节（二级）之下；只调整显示，不改原始转写。"""
    lines = text.splitlines(keepends=True)
    headings = []
    fence = None
    in_table = False
    for index, line in enumerate(lines):
        # 代码和 HTML 表格中的 # 是内容，不能当作文档标题改写。
        if fence:
            if re.fullmatch(
                r" {0,3}" + re.escape(fence[0]) + "{" + str(len(fence)) + r",}[ \t]*",
                line.rstrip("\r\n"),
            ):
                fence = None
            continue
        marker = re.match(r" {0,3}(`{3,}|~{3,})", line)
        if marker:
            fence = marker.group(1)
            continue
        if in_table or re.search(r"<table\b", line, re.I):
            in_table = not bool(re.search(r"</table\s*>", line, re.I))
            continue
        heading = re.match(r" {0,3}(#{1,6})(?=[ \t\r\n]|$)", line)
        if heading:
            headings.append((index, heading))
    if not headings:
        return text
    # 同一张图整体平移层级，保留父子关系；已有三级及以下标题不再加深。
    offset = max(0, 3 - min(len(match.group(1)) for _, match in headings))
    for index, match in headings:
        level = min(6, len(match.group(1)) + offset)
        lines[index] = (
            lines[index][: match.start(1)] + "#" * level + lines[index][match.end(1) :]
        )
    return "".join(lines)


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
            parts.append(
                f"[图片解读，非原生文本]\n\n{_render_transcription(image.transcription)}"
            )
        if image.purpose == "content" and image.read_status != "readable":
            status_label = {
                "pending": "待转写",
                "partial": "部分可读",
                "unreadable": "模型未能读清",
                "skipped": "已跳过",
                "failed": "转写处理失败",
            }[image.read_status]
            parts.append(f"图片读取状态：{status_label}")
        if image.warning:
            # reason 也可能是一般识别说明；不把正常读取依据统一标为限制。
            label = {
                "readable": "读取说明",
                "skipped": "跳过原因",
                "failed": "处理失败原因",
            }.get(image.read_status, "读取限制")
            parts.append(f"{label}：{image.warning}")
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
