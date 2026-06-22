"""只根据已落盘 JSON 为每个适用文件生成固定章节 Word 报告。"""

import os
import re
from pathlib import Path

from docx import Document

from app.models.documents import SourceRef
from app.models.results import AuditResult, FileAuditResult
from app.reports.json_report import read_json_result
from app.reports.styles import apply_document_styles, ordered_findings
from app.storage.file_store import FileStore


def _safe_stem(file_name: str) -> str:
    stem = Path(file_name).stem
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", stem)[:80] or "投标文件"


def collect_source_index(
    file_result: FileAuditResult,
) -> list[tuple[str, SourceRef]]:
    sources: list[tuple[str, SourceRef]] = []
    for finding in file_result.findings:
        sources.append(("规则依据", finding.rule_source))
        if finding.bid_source:
            sources.append(("投标响应", finding.bid_source))
    for review in file_result.evidence_results:
        sources.extend(("支撑材料", source) for source in review.evidence_found)
    unique: dict[tuple[str, str | None], tuple[str, SourceRef]] = {}
    for label, source in sources:
        unique.setdefault((source.file_id, source.chunk_id), (label, source))
    return list(unique.values())


def _add_findings(document, file_result: FileAuditResult) -> None:
    document.add_heading("审核明细", level=1)
    table = document.add_table(rows=1, cols=6)
    table.style = "Table Grid"
    headers = ["规则", "类别", "风险", "结论", "说明", "整改建议"]
    for index, value in enumerate(headers):
        table.rows[0].cells[index].text = value
    for finding in ordered_findings(file_result.findings):
        cells = table.add_row().cells
        values = [
            finding.title,
            str(finding.category),
            str(finding.severity),
            str(finding.result),
            finding.description,
            finding.suggestion or "",
        ]
        for index, value in enumerate(values):
            cells[index].text = value


def _render_report(
    result: AuditResult,
    file_result: FileAuditResult,
):
    document = Document()
    apply_document_styles(document)
    document.add_heading("电力智能招标审核报告", level=0)
    document.add_paragraph(f"审核任务：{result.task_id}")
    document.add_paragraph(f"投标文件：{file_result.file.file_name}")
    document.add_paragraph(f"总体结论：{file_result.overall_conclusion}")

    document.add_heading("项目适用性判断", level=1)
    document.add_paragraph(
        f"状态：{file_result.project_match.status}\n"
        f"理由：{file_result.project_match.reason}"
    )
    _add_findings(document, file_result)

    document.add_heading("资格与资料核验", level=1)
    if not file_result.evidence_results:
        document.add_paragraph("本文件未产生资料核验结果。")
    for review in file_result.evidence_results:
        document.add_heading(review.rule_id, level=2)
        document.add_paragraph(
            f"状态：{review.status}\n摘要：{review.summary}\n"
            f"缺失材料：{'、'.join(review.missing_evidence) or '无'}\n"
            f"一致性问题：{'、'.join(review.consistency_issues) or '无'}"
        )

    document.add_heading("不合规项和风险项", level=1)
    risks = [
        item
        for item in file_result.findings
        if str(item.result) in {"non_compliant", "risk"}
    ]
    for finding in risks:
        document.add_paragraph(f"{finding.title}：{finding.description}")
    if not risks:
        document.add_paragraph("无。")

    document.add_heading("无法判定与人工复核项", level=1)
    manual = [
        item for item in file_result.findings if str(item.result) == "manual_review"
    ]
    for finding in manual:
        document.add_paragraph(f"{finding.title}：{finding.description}")
    if not manual:
        document.add_paragraph("无。")

    document.add_heading("整改建议", level=1)
    for suggestion in file_result.suggestions:
        document.add_paragraph(suggestion, style="List Bullet")
    if not file_result.suggestions:
        document.add_paragraph("无。")

    document.add_heading("依据与证据索引", level=1)
    source_table = document.add_table(rows=1, cols=5)
    source_table.style = "Table Grid"
    for index, value in enumerate(["类型", "文件", "路径", "章节", "片段 ID"]):
        source_table.rows[0].cells[index].text = value
    for label, source in collect_source_index(file_result):
        cells = source_table.add_row().cells
        for index, value in enumerate(
            [
                label,
                source.file_name,
                source.source_path,
                source.section or "",
                source.chunk_id or "",
            ]
        ):
            cells[index].text = value
    return document


def generate_word_reports(
    json_path: str,
    file_store: FileStore | None = None,
) -> list[str]:
    """每次从正式 JSON 重新读取，Word 不接收内存中的未落盘结果。"""

    store = file_store or FileStore()
    result = read_json_result(json_path, store)
    directory = store.task_output_dir(result.task_id)
    paths: list[str] = []
    for file_result in result.file_results:
        if not file_result.is_applicable:
            continue
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
