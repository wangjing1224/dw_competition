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
async def test_invalid_section_edit_is_saved_but_cannot_finalize(tmp_path):
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
    receipt = await report.apply(
        "edit",
        None,
        [ReportEdit(old_text="## 总体总结", new_text="## 别的章节")],
        "edit-2",
    )
    assert "草稿已保存" in receipt and "校验未通过" in receipt
    assert report.read() == saved.replace("## 总体总结", "## 别的章节")
    with pytest.raises(ValueError, match="类别或规则集合"):
        report.finish()
    await report.apply(
        "edit",
        None,
        [ReportEdit(old_text="## 别的章节", new_text="## 总体总结")],
        "repair",
    )
    assert report.finish().report_title == "技术投标文件审核报告"


@pytest.mark.asyncio
async def test_io_failure_is_not_reported_as_a_saved_invalid_draft(
    tmp_path, monkeypatch
):
    context = audit_context()
    report = ReportDocument(
        tmp_path / "draft.md", context.rule_set, SourceLedger(context.parsed_documents)
    )
    before = report.read()

    def fail_write(*args):
        raise OSError("disk full")

    monkeypatch.setattr("app.reports.markdown_report.atomic_write", fail_write)
    with pytest.raises(OSError, match="disk full"):
        await report.apply("write", filled_report(context.rule_set), None, "save")
    assert report.read() == before
    assert "保存错误" in report.progress()
    with pytest.raises(ValueError, match="保存错误"):
        report.finish()


@pytest.mark.asyncio
@pytest.mark.parametrize("version", [1, 2])
async def test_report_status_is_only_in_tool_prefix_and_reads_are_current(
    tmp_path, version
):
    context = audit_context()
    report = ReportDocument(
        tmp_path / "draft.md",
        context.rule_set,
        SourceLedger(context.parsed_documents),
        version=version,
    )
    separator = "\n\n--- 以下为当前草稿正文 ---\n\n"
    original = report.read()
    first_read = await report.apply("read", None, None, "read-1")
    assert first_read.startswith("## 当前草稿状态\n已填写规则 0/1")
    assert first_read.split(separator, 1)[1] == original
    assert report.read() == original

    text = filled_report(context.rule_set, title="初稿审核报告", version=version)
    saved = await report.apply("write", text, None, "write-1")
    assert saved.startswith("## 当前草稿状态\n已填写规则 1/1")
    assert "仍须完成全部阅读范围并通过定稿校验" in saved
    assert "\n\n## 本次操作\n草稿已保存。" in saved
    assert text not in saved and separator not in saved
    assert report.read() == text
    assert report.finish().report_title == "初稿审核报告"

    edits = [ReportEdit(old_text="# 初稿审核报告", new_text="# 修订审核报告")]
    edited = await report.apply("edit", None, edits, "edit-1")
    updated = text.replace("# 初稿审核报告", "# 修订审核报告")
    assert edited.startswith("## 当前草稿状态\n") and "完成1处替换" in edited
    assert "修订审核报告" not in edited  # 保存回执不回传变更正文。
    assert report.read() == updated
    assert report.finish().report_title == "修订审核报告"

    # 仅写/编辑缓存原回执，重放不能再次覆盖或替换当前稿。
    assert await report.apply("write", text, None, "write-1") == saved
    assert await report.apply("edit", None, edits, "edit-1") == edited
    assert report.read() == updated
    reread = await report.apply("read", None, None, "read-1")
    assert reread != first_read and reread.split(separator, 1)[1] == updated
    assert reread.count("## 当前草稿状态") == 1
    assert "## 当前草稿状态" not in report.read()


@pytest.mark.asyncio
async def test_invalid_draft_read_and_edit_use_status_prefix_without_saving_header(
    tmp_path,
):
    context = audit_context()
    report = ReportDocument(
        tmp_path / "draft.md",
        context.rule_set,
        SourceLedger(context.parsed_documents),
        version=2,
    )
    text = filled_report(context.rule_set, version=2) + "\n投标片段：fake\n"
    saved = await report.apply("write", text, None, "invalid")
    read = await report.apply("read", None, None, "read")
    for reply in (saved, read):
        assert reply.startswith("## 当前草稿状态\n草稿校验未通过：")
        assert "fake" in reply and "已填写规则" not in reply
    assert read.split("--- 以下为当前草稿正文 ---\n\n", 1)[1] == text
    assert report.read() == text
    with pytest.raises(ValueError, match="未实际读取"):
        report.finish()

    # 状态首部不属于正文，不得被当作一次成功编辑。
    with pytest.raises(ValueError, match="未唯一命中"):
        await report.apply(
            "edit",
            None,
            [ReportEdit(old_text="## 当前草稿状态", new_text="已修正")],
            "wrong-target",
        )
    assert report.read() == text
    assert "未解决的保存错误" in await report.apply("read", None, None, "read-again")
    with pytest.raises(ValueError, match="保存错误"):
        report.finish()

    repaired = await report.apply(
        "edit",
        None,
        [ReportEdit(old_text="\n投标片段：fake\n", new_text="")],
        "repair",
    )
    assert repaired.startswith("## 当前草稿状态\n已填写规则 1/1")
    assert report.finish()
