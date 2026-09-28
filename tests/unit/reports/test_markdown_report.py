"""报告标题可由模型修改，业务章节和完成校验仍由代码约束。"""

import pytest

from app.audit.sources import SourceLedger
from app.reports.markdown_report import TITLE_PLACEHOLDER, ReportDocument, parse_report
from app.tools.report_tools import ReportEdit
from tests.helpers.audit import audit_context, filled_report


def test_unfilled_title_is_allowed_in_draft_but_not_final():
    context = audit_context()
    ledger = SourceLedger(context.parsed_documents)
    text = filled_report(context.rule_set, title=TITLE_PLACEHOLDER)
    decision, missing = parse_report(text, context.rule_set, ledger)
    assert decision is None and missing == ["报告标题"]
    with pytest.raises(ValueError, match="报告未完成：报告标题"):
        parse_report(text, context.rule_set, ledger, final=True)


@pytest.mark.parametrize(
    "title",
    [
        "",
        "   ",
        "长" * 121,
        "〈项目名称〉审核报告",
        "标题\n第二行",
        "标题\n# 另一个标题",
    ],
)
def test_invalid_title_is_rejected(title):
    context = audit_context()
    with pytest.raises(ValueError):
        parse_report(
            filled_report(context.rule_set, title=title),
            context.rule_set,
            SourceLedger(context.parsed_documents),
            final=True,
        )


@pytest.mark.asyncio
async def test_title_edit_is_saved_without_allowing_section_changes(tmp_path):
    context = audit_context()
    report = ReportDocument(
        tmp_path / "draft.md", context.rule_set, SourceLedger(context.parsed_documents)
    )
    await report.apply(
        "write", filled_report(context.rule_set, title="初稿审核报告"), None, "write-1"
    )
    await report.apply(
        "edit",
        None,
        [ReportEdit(old_text="# 初稿审核报告", new_text="# 技术投标文件审核报告")],
        "edit-1",
    )
    assert report.finish().report_title == "技术投标文件审核报告"
    saved = report.read()
    with pytest.raises(ValueError, match="类别或规则集合"):
        await report.apply(
            "edit",
            None,
            [ReportEdit(old_text="## 总体总结", new_text="## 别的章节")],
            "edit-2",
        )
    assert report.read() == saved
