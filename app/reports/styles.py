"""Word 报告字体、表格样式及 Finding 固定排序。"""

from docx.document import Document as WordDocument
from docx.oxml.ns import qn
from docx.shared import Pt

def apply_document_styles(document: WordDocument) -> None:
    """统一设置中文字体；真实单位模板后续可在此处替换。"""

    normal = document.styles["Normal"]
    normal.font.name = "宋体"
    normal._element.rPr.rFonts.set(qn("w:eastAsia"), "宋体")
    normal.font.size = Pt(10.5)
    for style_name in ("Title", "Heading 1", "Heading 2"):
        style = document.styles[style_name]
        style.font.name = "黑体"
        style._element.rPr.rFonts.set(qn("w:eastAsia"), "黑体")
