"""根据已校验 JSON 生成正式中文 Word 审核报告。"""

from __future__ import annotations

import os
import re
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.shared import Pt, RGBColor

from app.core.config import get_settings
from app.models.documents import SourceRef
from app.models.results import AuditResult, FileAuditResult
from app.models.rules import RuleReportItem
from app.reports.json_report import read_json_result
from app.reports.styles import (
    ACCENT,
    DARK,
    MUTED,
    apply_document_styles,
    category_label,
    conclusion_label,
    evidence_type_label,
    finding_label,
    format_table_header,
    match_label,
    priority_label,
    result_fill,
    set_table_geometry,
    shade_cell,
)
from app.retrieval.index import embedding_config_fingerprint
from app.retrieval.store import load_index
from app.storage.file_store import FileStore


CATEGORY_ORDER = {
    "format": 0,
    "qualification": 1,
    "commercial": 2,
    "technical": 3,
    "performance_capacity": 4,
    "consistency_risk": 5,
}
PRIORITY_ORDER = {"critical": 0, "important": 1, "normal": 2}
FINDING_ORDER = {
    "non_compliant": 0,
    "risk": 1,
    "manual_review": 2,
    "compliant": 3,
}


def _safe_stem(file_name: str) -> str:
    stem = Path(file_name).stem
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", stem)[:80] or "投标文件"


def _index_chunks(scope_id: str) -> dict[str, object]:
    try:
        settings = get_settings()
        loaded = load_index(
            scope_id,
            expected_fingerprint=embedding_config_fingerprint(
                settings.embedding_model
            ),
        )
        return {chunk.chunk_id: chunk for chunk in loaded.chunks}
    except Exception:
        # 报告正文不能因为索引被清理而失败；JSON 中仍保留 Chunk ID。
        return {}


def _source_indexes(result: AuditResult) -> dict[str, dict[str, object]]:
    return {
        "招标原文": _index_chunks(f"rules-{result.project_id}"),
        "投标原文": _index_chunks(f"bid-{result.task_id}"),
        "支撑资料": _index_chunks(
            f"evidence-{result.project_id}-{result.bidder_id}"
        ),
    }


def collect_source_index(
    result: AuditResult,
    file_result: FileAuditResult,
) -> list[tuple[str, SourceRef]]:
    indexes = _source_indexes(result)
    collected: list[tuple[str, SourceRef]] = []
    seen: set[tuple[str, str]] = set()
    for finding in file_result.findings:
        fields = (
            ("招标原文", finding.tender_chunk_ids),
            ("投标原文", finding.bid_chunk_ids),
            ("支撑资料", finding.evidence_chunk_ids),
        )
        for label, chunk_ids in fields:
            for chunk_id in chunk_ids:
                key = (label, chunk_id)
                if key in seen:
                    continue
                seen.add(key)
                chunk = indexes[label].get(chunk_id)
                if chunk is not None:
                    collected.append((label, chunk.source_ref(500)))
    return collected


def _ordered_findings(
    file_result: FileAuditResult,
    rules: dict[str, RuleReportItem],
):
    return sorted(
        file_result.findings,
        key=lambda finding: (
            CATEGORY_ORDER.get(str(rules[finding.rule_id].category), 99),
            PRIORITY_ORDER.get(str(rules[finding.rule_id].priority), 99),
            FINDING_ORDER.get(str(finding.result), 99),
            finding.rule_id,
        ),
    )


def _set_run(
    run,
    *,
    chinese: str = "宋体",
    latin: str = "Arial",
    size: float | None = None,
    color: str | None = None,
    bold: bool | None = None,
) -> None:
    from docx.oxml.ns import qn

    run.font.name = latin
    run._element.rPr.rFonts.set(qn("w:ascii"), latin)
    run._element.rPr.rFonts.set(qn("w:hAnsi"), latin)
    run._element.rPr.rFonts.set(qn("w:eastAsia"), chinese)
    if size is not None:
        run.font.size = Pt(size)
    if color is not None:
        run.font.color.rgb = RGBColor.from_string(color)
    if bold is not None:
        run.bold = bold


def _add_label_value(document, label: str, value: str, *, style=None) -> None:
    paragraph = document.add_paragraph(style=style)
    label_run = paragraph.add_run(f"{label}：")
    _set_run(label_run, chinese="黑体", bold=True, color=DARK)
    value_run = paragraph.add_run(value or "未记录")
    _set_run(value_run)


def _add_title_block(
    document,
    result: AuditResult,
    file_result: FileAuditResult,
) -> None:
    kicker = document.add_paragraph()
    kicker.paragraph_format.space_before = Pt(18)
    kicker.paragraph_format.space_after = Pt(6)
    run = kicker.add_run("自动审核结果")
    _set_run(run, chinese="黑体", size=10, bold=True, color=ACCENT)

    title = document.add_paragraph(style="Title")
    title.add_run("电力智能招标审核报告")
    subtitle = document.add_paragraph()
    subtitle.paragraph_format.space_after = Pt(14)
    run = subtitle.add_run(file_result.file.file_name)
    _set_run(run, chinese="黑体", size=14, color=MUTED)

    metadata = (
        ("审核任务编号", result.task_id),
        ("项目编号", result.project_id),
        ("投标方编号", result.bidder_id),
        ("生成时间", datetime.now().strftime("%Y年%m月%d日 %H:%M")),
    )
    for label, value in metadata:
        _add_label_value(document, label, value, style="Report Metadata")

    spacer = document.add_paragraph()
    spacer.paragraph_format.space_after = Pt(6)


def _add_summary_markdown(document, text: str) -> None:
    """渲染摘要中实际出现的最小 Markdown 子集。"""

    paragraph_buffer: list[str] = []

    def flush() -> None:
        if paragraph_buffer:
            value = re.sub(r"\*\*(.+?)\*\*", r"\1", " ".join(paragraph_buffer))
            document.add_paragraph(value.strip())
            paragraph_buffer.clear()

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            flush()
            continue
        heading = re.match(r"^(#{2,3})\s+(.+)$", line)
        if heading:
            flush()
            level = 2 if len(heading.group(1)) == 2 else 3
            document.add_heading(
                re.sub(r"\*\*(.+?)\*\*", r"\1", heading.group(2)),
                level=level,
            )
            continue
        field = re.match(r"^-\s+\*\*(.+?)\*\*[：:]\s*(.*)$", line)
        if field:
            flush()
            _add_label_value(document, field.group(1), field.group(2))
            continue
        bullet = re.match(r"^-\s+(.+)$", line)
        if bullet:
            flush()
            value = re.sub(r"\*\*(.+?)\*\*", r"\1", bullet.group(1))
            document.add_paragraph(value, style="List Bullet")
            continue
        paragraph_buffer.append(line)
    flush()


def _add_overview(
    document,
    file_result: FileAuditResult,
) -> None:
    document.add_heading("一、审核结论概览", level=1)
    _add_label_value(
        document,
        "总体结论",
        conclusion_label(file_result.overall_conclusion),
    )
    counts = Counter(str(item.result) for item in file_result.findings)
    table = document.add_table(rows=2, cols=4)
    table.style = "Table Grid"
    labels = ("符合", "不符合", "存在风险", "需人工复核")
    keys = ("compliant", "non_compliant", "risk", "manual_review")
    for index, label in enumerate(labels):
        table.rows[0].cells[index].text = label
        table.rows[1].cells[index].text = str(counts[keys[index]])
        table.rows[1].cells[index].paragraphs[0].alignment = (
            WD_ALIGN_PARAGRAPH.CENTER
        )
        shade_cell(table.rows[1].cells[index], result_fill(keys[index]))
    set_table_geometry(table, [2340, 2340, 2340, 2340])
    format_table_header(table.rows[0])
    if file_result.audit_summary:
        document.add_heading("总体审核总结", level=2)
        document.add_paragraph(file_result.audit_summary)


def _add_match_section(document, file_result: FileAuditResult) -> None:
    document.add_heading("二、项目匹配情况", level=1)
    _add_label_value(
        document,
        "匹配状态",
        match_label(file_result.project_match.status),
    )
    _add_label_value(document, "判断理由", file_result.project_match.reason)


def _add_manual_materials(
    document,
    result: AuditResult,
) -> None:
    manual_materials = [
        item
        for item in result.evidence_classifications
        if str(item.classification_status) == "manual_review"
    ]
    if not manual_materials:
        return
    document.add_heading("需人工读取的投标方资料", level=2)
    for item in manual_materials:
        document.add_paragraph(
            f"{item.file_name}（{evidence_type_label(item.evidence_type)}）："
            f"{item.reason}；{item.bid_summary}",
            style="List Bullet",
        )


def _add_category_summaries(
    document,
    file_result: FileAuditResult,
) -> None:
    document.add_heading("四、分类审核总结", level=1)
    if not file_result.category_summaries:
        document.add_paragraph("未形成分类审核总结。")
        return
    for category, summary in sorted(
        file_result.category_summaries.items(),
        key=lambda item: CATEGORY_ORDER.get(item[0], 99),
    ):
        document.add_heading(category_label(category), level=2)
        document.add_paragraph(summary)


def _add_key_issues(
    document,
    file_result: FileAuditResult,
    rules: dict[str, RuleReportItem],
) -> None:
    document.add_heading("五、重点问题清单", level=1)
    issues = [
        finding
        for finding in _ordered_findings(file_result, rules)
        if str(finding.result) != "compliant"
    ]
    if not issues:
        document.add_paragraph("未发现不符合项、风险项或需人工复核项。")
        return
    table = document.add_table(rows=1, cols=4)
    table.style = "Table Grid"
    for index, value in enumerate(["规则", "类别", "结论", "审核说明"]):
        table.rows[0].cells[index].text = value
    set_table_geometry(table, [900, 1800, 1300, 5360])
    format_table_header(table.rows[0])
    for finding in issues:
        rule = rules[finding.rule_id]
        cells = table.add_row().cells
        values = (
            finding.rule_id,
            category_label(rule.category),
            finding_label(finding.result),
            finding.summary,
        )
        for index, value in enumerate(values):
            cells[index].text = value
        cells[0].paragraphs[0].alignment = WD_ALIGN_PARAGRAPH.CENTER
        cells[2].paragraphs[0].alignment = WD_ALIGN_PARAGRAPH.CENTER
        shade_cell(cells[2], result_fill(finding.result))
    set_table_geometry(table, [900, 1800, 1300, 5360])


def _finding_sources(
    finding,
    indexes: dict[str, dict[str, object]],
) -> list[str]:
    values: list[str] = []
    for label, chunk_ids in (
        ("招标原文", finding.tender_chunk_ids),
        ("投标原文", finding.bid_chunk_ids),
        ("支撑资料", finding.evidence_chunk_ids),
    ):
        for chunk_id in chunk_ids:
            chunk = indexes[label].get(chunk_id)
            if chunk is None:
                values.append(f"{label}：片段编号 {chunk_id}")
                continue
            source = chunk.source_ref(0)
            location = " / ".join(
                value
                for value in (source.file_name, source.section)
                if value
            )
            values.append(f"{label}：{location or source.source_path}")
    return values


def _add_finding_details(
    document,
    result: AuditResult,
    file_result: FileAuditResult,
    rules: dict[str, RuleReportItem],
) -> None:
    document.add_heading("六、逐规则审核明细", level=1)
    indexes = _source_indexes(result)
    grouped: dict[str, list] = defaultdict(list)
    for finding in _ordered_findings(file_result, rules):
        grouped[str(rules[finding.rule_id].category)].append(finding)
    for category in sorted(
        grouped,
        key=lambda value: CATEGORY_ORDER.get(value, 99),
    ):
        document.add_heading(category_label(category), level=2)
        for finding in grouped[category]:
            rule = rules[finding.rule_id]
            heading = document.add_heading(level=3)
            heading.add_run(
                f"{finding.rule_id}｜{priority_label(rule.priority)}｜"
                f"{finding_label(finding.result)}"
            )
            _add_label_value(document, "审核要求", rule.requirement)
            _add_label_value(document, "审核结论", finding.summary)
            sources = _finding_sources(finding, indexes)
            if sources:
                label = document.add_paragraph(style="Finding Label")
                label.add_run("引用依据")
                for value in sources:
                    document.add_paragraph(value, style="List Bullet")


def _add_suggestions(
    document,
    file_result: FileAuditResult,
) -> None:
    document.add_heading("七、整改与复核建议", level=1)
    if not file_result.suggestions:
        document.add_paragraph("暂无额外整改建议。")
        return
    for suggestion in file_result.suggestions:
        document.add_paragraph(suggestion, style="List Bullet")


def _add_source_appendix(
    document,
    result: AuditResult,
    file_result: FileAuditResult,
) -> None:
    document.add_heading("八、依据与证据索引", level=1)
    sources = collect_source_index(result, file_result)
    if not sources:
        document.add_paragraph("本次结论未引用可解析的工具召回片段。")
        return
    for index, (label, source) in enumerate(sources, start=1):
        paragraph = document.add_paragraph()
        paragraph.paragraph_format.space_after = Pt(2)
        run = paragraph.add_run(
            f"{index}. {label}｜{source.file_name}"
            f"{'｜' + source.section if source.section else ''}"
        )
        _set_run(run, chinese="黑体", bold=True, color=ACCENT)
        if source.quote:
            quote = document.add_paragraph(source.quote)
            quote.paragraph_format.left_indent = Pt(16)
            quote.paragraph_format.space_after = Pt(8)
            for run in quote.runs:
                _set_run(run, size=9.5, color=MUTED)


def _render_report(
    result: AuditResult,
    file_result: FileAuditResult,
):
    rules = {rule.rule_id: rule for rule in result.rules}
    document = Document()
    apply_document_styles(document)
    _add_title_block(document, result, file_result)
    _add_overview(document, file_result)
    _add_match_section(document, file_result)

    if not file_result.is_applicable:
        document.add_heading("三、不进入自动审核的原因", level=1)
        document.add_paragraph(
            file_result.exclusion_reason or "未记录具体原因"
        )
        if file_result.document_summary is not None:
            document.add_heading("四、文件摘要", level=1)
            _add_summary_markdown(
                document,
                file_result.document_summary.summary,
            )
        return document

    document.add_heading("三、投标文件摘要", level=1)
    if file_result.document_summary is None:
        document.add_paragraph("未生成投标文件摘要。")
    else:
        _add_summary_markdown(document, file_result.document_summary.summary)
    _add_manual_materials(document, result)
    _add_category_summaries(document, file_result)
    _add_key_issues(document, file_result, rules)
    _add_finding_details(document, result, file_result, rules)
    _add_suggestions(document, file_result)
    _add_source_appendix(document, result, file_result)
    return document


def generate_word_reports(
    json_path: str,
    file_store: FileStore | None = None,
) -> list[str]:
    store = file_store or FileStore()
    result = read_json_result(json_path, store)
    directory = store.task_output_dir(result.task_id)
    paths: list[str] = []
    for file_result in result.file_results:
        path = directory / (
            f"{_safe_stem(file_result.file.file_name)}_{file_result.file.file_id[:12]}"
            "_审核报告.docx"
        )
        temporary = path.with_suffix(".docx.tmp")
        document = _render_report(result, file_result)
        document.save(temporary)
        os.replace(temporary, path)
        paths.append(store.relative_to_data_root(path))
    return paths
