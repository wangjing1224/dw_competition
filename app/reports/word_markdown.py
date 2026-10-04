"""Markdown 和解析摘录表格到原生 Word；不执行 HTML 或请求外部图片。"""

import re
from urllib.parse import unquote, urlsplit

from docx.opc.constants import RELATIONSHIP_TYPE as RT
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Pt, RGBColor
from lxml import etree
from markdown_it import MarkdownIt
from markdown_it.tree import SyntaxTreeNode

from app.reports.presentation import heading_anchor
from app.reports.styles import (
    ACCENT,
    LIGHT_FILL,
    TABLE_WIDTH_DXA,
    format_table_header,
    set_table_geometry,
)


def _excerpt_table_block(state, start, end, silent):
    """仅识别完整的 HTML 表格块；其余 HTML 与代码仍按字面处理。"""
    pos = state.bMarks[start] + state.tShift[start]
    if state.is_code_block(start) or not re.match(
        r"<table(?:\s|>)", state.src[pos:state.eMarks[start]], re.I
    ):
        return False
    for line in range(start, end):
        if state.sCount[line] < state.blkIndent:
            break
        value = state.src[state.bMarks[line] + state.tShift[line]:state.eMarks[line]]
        if re.search(r"</table\s*>", value, re.I):
            if not silent:
                state.line = line + 1
                token = state.push("excerpt_table", "", 0)
                token.map = [start, state.line]
                token.content = state.getLines(start, state.line, state.blkIndent, True)
            return True
    return False


def _html_cell(paragraph, element, *, bold=False, italic=False):
    """只投影文字、换行和强调；忽略样式、脚本及图片地址。"""
    def add(text):
        if text:
            run = paragraph.add_run(text)
            run.bold, run.italic = bold, italic

    add(element.text)
    for child in element:
        if child.tag == "br":
            add("\n")
        elif child.tag == "img":
            add(child.get("alt", ""))
        elif isinstance(child.tag, str) and child.tag not in {"script", "style"}:
            if child.tag in {"p", "div", "tr", "td", "th"} and paragraph.text:
                add("\n")
            _html_cell(
                paragraph, child,
                bold=bold or child.tag in {"b", "strong"},
                italic=italic or child.tag in {"i", "em"},
            )
        add(child.tail)


def _render_excerpt_table(container, html):
    # lxml 已由 python-docx 使用；仅在内存中解析，不加载 URL 或外部资源。
    root = etree.HTML(html, parser=etree.HTMLParser(no_network=True))
    rows = root.xpath(
        "./body/table/tr | ./body/table/thead/tr | "
        "./body/table/tbody/tr | ./body/table/tfoot/tr"
    )
    placements, occupied = [], set()
    try:
        for r, row in enumerate(rows):
            c = 0
            for cell in row.xpath("./td | ./th"):
                while (r, c) in occupied:
                    c += 1
                rs, cs = int(cell.get("rowspan", 1)), int(cell.get("colspan", 1))
                # Word 表格最多 63 列；异常跨度不扩张成巨大的内存网格。
                if not (1 <= rs <= len(rows) - r and 1 <= cs <= 63 - c):
                    raise ValueError("无效表格跨度")
                area = {(y, x) for y in range(r, r + rs) for x in range(c, c + cs)}
                if occupied & area:
                    raise ValueError("表格单元格重叠")
                occupied.update(area)
                placements.append((r, c, rs, cs, cell))
                c += cs
        columns = max(c for _, c in occupied) + 1
    except ValueError:
        # 非规范摘录仍保留可读内容，不伪造合并关系或令整份报告导出失败。
        container.add_paragraph("原文表格结构无法还原，以下按文字列出：")
        _html_cell(container.add_paragraph(), root)
        return

    table = container.add_table(rows=len(rows), cols=columns)
    table.style = "Table Grid"
    # 为序号、数量等短列少分宽度，给要求/响应正文留空间；总宽保持报告版心。
    weights = [4] * columns
    for _, c, _, cs, cell in placements:
        weight = min(40, max((len(t) for t in cell.itertext()), default=0) / cs)
        for index in range(c, c + cs):
            weights[index] = max(weights[index], weight)
    minimum = min(720, TABLE_WIDTH_DXA // columns)
    spare = TABLE_WIDTH_DXA - minimum * columns
    widths = [minimum + int(spare * w / sum(weights)) for w in weights]
    widths[-1] += TABLE_WIDTH_DXA - sum(widths)
    set_table_geometry(table, widths)  # 先设置网格宽度，再合并，保留合并后的总宽。
    for r, c, rs, cs, source in placements:
        cell = table.cell(r, c)
        if rs > 1 or cs > 1:
            cell = cell.merge(table.cell(r + rs - 1, c + cs - 1))
        p = cell.paragraphs[0]
        _html_cell(p, source)
        p.paragraph_format.space_after = Pt(4)
        p.paragraph_format.line_spacing = 1.15
        for run in p.runs:
            run.font.size = Pt(9 if columns >= 6 else 10)
    # 合并表头跨行时，重复整个表头区域，避免跨页时拆断纵向合并。
    header_end = 1
    for r, _, rs, _, _ in placements:
        if r < header_end:
            header_end = max(header_end, r + rs)
    for row in table.rows[:header_end]:
        format_table_header(row)
    for row in table.rows:
        # 普通行尽量整行跨页，避免续页只剩某个长单元格的两行尾句。
        row._tr.get_or_add_trPr().append(OxmlElement("w:cantSplit"))


def _numbering(document, ordered, start=1):
    root = document.part.numbering_part.element
    abstract_id = (
        max(
            [
                int(x.get(qn("w:abstractNumId")))
                for x in root.findall(qn("w:abstractNum"))
            ]
            + [-1]
        )
        + 1
    )
    num_id = (
        max([int(x.get(qn("w:numId"))) for x in root.findall(qn("w:num"))] + [0]) + 1
    )
    abstract = OxmlElement("w:abstractNum")
    abstract.set(qn("w:abstractNumId"), str(abstract_id))
    level = OxmlElement("w:lvl")
    level.set(qn("w:ilvl"), "0")
    for tag, val in (
        ("start", str(start)),
        ("numFmt", "decimal" if ordered else "bullet"),
        ("lvlText", "%1." if ordered else "•"),
    ):
        elem = OxmlElement("w:" + tag)
        elem.set(qn("w:val"), val)
        level.append(elem)
    abstract.append(level)
    root.append(abstract)
    num = OxmlElement("w:num")
    num.set(qn("w:numId"), str(num_id))
    ref = OxmlElement("w:abstractNumId")
    ref.set(qn("w:val"), str(abstract_id))
    num.append(ref)
    root.append(num)
    return num_id


def _inline(paragraph, tokens, *, bold=False, italic=False, link=None, anchors=None):
    for t in tokens:
        if t.type == "strong_open":
            bold = True
        elif t.type == "strong_close":
            bold = False
        elif t.type == "em_open":
            italic = True
        elif t.type == "em_close":
            italic = False
        elif t.type == "link_open":
            link = t.attrGet("href")
        elif t.type == "link_close":
            link = None
        elif t.type in {
            "text",
            "code_inline",
            "html_inline",
            "image",
            "softbreak",
            "hardbreak",
        }:
            text = "\n" if t.type in {"softbreak", "hardbreak"} else t.content
            run = paragraph.add_run(text)
            run.bold, run.italic = bold, italic
            if t.type == "code_inline":
                run.font.name = "Courier New"
            if link and (
                link.startswith("#")
                or urlsplit(link).scheme in {"http", "https", "mailto"}
            ):
                hyperlink = OxmlElement("w:hyperlink")
                if link.startswith("#"):
                    anchor = unquote(link[1:])
                    hyperlink.set(qn("w:anchor"), (anchors or {}).get(anchor, anchor))
                else:
                    hyperlink.set(
                        qn("r:id"),
                        paragraph.part.relate_to(link, RT.HYPERLINK, is_external=True),
                    )
                run.font.color.rgb = RGBColor.from_string(ACCENT)
                hyperlink.append(run._r)
                paragraph._p.append(hyperlink)


def render_markdown(document, text):
    parser = MarkdownIt("commonmark", {"html": False}).enable("table")
    parser.block.ruler.before(
        "html_block", "excerpt_table", _excerpt_table_block,
        {"alt": ["paragraph", "reference", "blockquote"]},
    )
    root = SyntaxTreeNode(parser.parse(text))
    bookmark_count = len(document.element.findall(".//" + qn("w:bookmarkStart")))
    anchors = {}
    for title in re.findall(r"^### (4\.\d+ .+)$", text, re.MULTILINE):
        anchors[heading_anchor(title)] = "report-rule-" + title.split(" ", 1)[
            0
        ].replace(".", "-")

    def paragraph(container, node, quote=False, numbering=None, depth=0):
        p = container.add_paragraph()
        if node.type == "heading" and not quote:
            level = int(node.tag[1:])
            p.style = "Title" if level == 1 else f"Heading {min(level - 1, 3)}"
        if numbering:
            props = p._p.get_or_add_pPr().get_or_add_numPr()
            props.get_or_add_ilvl().val = 0
            props.get_or_add_numId().val = numbering
            p.paragraph_format.left_indent = Cm(0.65 * max(1, depth))
            p.paragraph_format.first_line_indent = Cm(-0.35)
        for child in node.children or []:
            if child.type == "inline":
                _inline(p, child.token.children or [], anchors=anchors)
        if node.type in {"fence", "code_block"}:
            p.add_run(node.content).font.name = "Courier New"
        if quote:
            p.paragraph_format.left_indent = Cm(0.4)
            p.paragraph_format.right_indent = Cm(0.2)
            p.paragraph_format.space_after = Pt(4)
            shade = OxmlElement("w:shd")
            shade.set(qn("w:fill"), LIGHT_FILL)
            p._p.get_or_add_pPr().append(shade)
        if node.type == "heading" and not quote:
            match = re.match(r"4\.(\d+) ", p.text)
            if match:
                nonlocal bookmark_count
                bookmark_count += 1
                start, end = (
                    OxmlElement("w:bookmarkStart"),
                    OxmlElement("w:bookmarkEnd"),
                )
                start.set(qn("w:id"), str(bookmark_count))
                start.set(qn("w:name"), f"report-rule-4-{match[1]}")
                end.set(qn("w:id"), str(bookmark_count))
                p._p.insert(1 if p._p.pPr is not None else 0, start)
                p._p.append(end)
        if node.type == "paragraph" and len(node.children or []) == 1:
            children = node.children[0].token.children or []
            if (
                children
                and children[0].type == "strong_open"
                and children[-1].type == "strong_close"
            ):
                p.paragraph_format.keep_with_next = True
        return p

    def render(node, container, quote=False, depth=0):
        if node.type == "excerpt_table":
            # 表后同一行的文字或相邻表格也保留，不交给 HTML 吞掉 Markdown。
            parts = re.split(
                r"(<table\b[^>]*>.*?</table\s*>)", node.content, flags=re.I | re.S
            )
            for part in parts:
                if re.match(r"<table(?:\s|>)", part, re.I):
                    _render_excerpt_table(container, part)
                elif part.strip():
                    for child in SyntaxTreeNode(parser.parse(part)).children:
                        render(child, container, quote, depth)
        elif node.type in {"paragraph", "heading", "code_block", "fence"}:
            paragraph(container, node, quote, depth=depth)
        elif node.type in {"ordered_list", "bullet_list"}:
            numbering = _numbering(
                document, node.type == "ordered_list", int(node.attrs.get("start", 1))
            )
            for item in node.children:
                numbered = False
                for child in item.children:
                    if child.type == "paragraph" and not numbered:
                        paragraph(container, child, quote, numbering, depth + 1)
                        numbered = True
                    else:
                        render(child, container, quote, depth + 1)
        elif node.type == "table":
            rows = [row for section in node.children for row in section.children]
            columns = max(len(row.children) for row in rows)
            table = container.add_table(rows=0, cols=columns)
            table.style = "Table Grid"
            for row in rows:
                cells = table.add_row().cells
                for i, cell in enumerate(row.children):
                    for child in cell.children:
                        if child.type == "inline":
                            _inline(
                                cells[i].paragraphs[0],
                                child.token.children or [],
                                anchors=anchors,
                            )
            width = TABLE_WIDTH_DXA // columns
            set_table_geometry(
                table,
                [width] * (columns - 1) + [TABLE_WIDTH_DXA - width * (columns - 1)],
            )
            format_table_header(table.rows[0])
        else:
            for child in node.children or []:
                render(child, container, quote or node.type == "blockquote", depth)

    for node in root.children:
        render(node, document)
