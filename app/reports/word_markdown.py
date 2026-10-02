"""Markdown AST 到原生 Word；不执行 HTML，也不请求任何外部图片。"""

import re
from urllib.parse import unquote, urlsplit

from docx.opc.constants import RELATIONSHIP_TYPE as RT
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Pt, RGBColor
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
    root = SyntaxTreeNode(
        MarkdownIt("commonmark", {"html": False}).enable("table").parse(text)
    )
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
        if node.type in {"paragraph", "heading", "code_block", "fence"}:
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
