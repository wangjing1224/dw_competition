"""Word 报告字体、表格样式及 Finding 固定排序。"""

from docx.document import Document as WordDocument
from docx.oxml.ns import qn
from docx.shared import Pt

from app.models.findings import Finding


CATEGORY_ORDER = {
    "format": 0,
    "qualification": 1,
    "commercial": 2,
    "technical": 3,
    "performance_capacity": 4,
    "consistency_risk": 5,
}
SEVERITY_ORDER = {
    "fatal": 0,
    "major": 1,
    "minor": 2,
    "unknown": 3,
    "info": 4,
}


def ordered_findings(findings: list[Finding]) -> list[Finding]:
    return sorted(
        findings,
        key=lambda item: (
            CATEGORY_ORDER.get(str(item.category), 99),
            SEVERITY_ORDER.get(str(item.severity), 99),
            item.rule_id,
        ),
    )


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
