"""只根据已落盘 JSON 生成与轻量 Finding 一致的 Word 报告。"""

import os
import re
from pathlib import Path

from docx import Document

from app.core.config import get_settings
from app.models.documents import SourceRef
from app.models.results import AuditResult, FileAuditResult
from app.models.rules import RuleReportItem
from app.reports.json_report import read_json_result
from app.reports.styles import apply_document_styles
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
        # 报告正文不能因为索引被清理而完全失败；JSON 中仍保留 chunk ID。
        return {}


def collect_source_index(
    result: AuditResult,
    file_result: FileAuditResult,
) -> list[tuple[str, SourceRef]]:
    indexes = {
        "招标原文": _index_chunks(f"rules-{result.project_id}"),
        "投标原文": _index_chunks(f"bid-{result.task_id}"),
        "支撑资料": _index_chunks(
            f"evidence-{result.project_id}-{result.bidder_id}"
        ),
    }
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
            finding.rule_id,
        ),
    )


def _add_findings(
    document,
    file_result: FileAuditResult,
    rules: dict[str, RuleReportItem],
) -> None:
    document.add_heading("审核明细", level=1)
    table = document.add_table(rows=1, cols=5)
    table.style = "Table Grid"
    headers = ["审核规则", "类别", "重要程度", "结论", "审核说明"]
    for index, value in enumerate(headers):
        table.rows[0].cells[index].text = value
    for finding in _ordered_findings(file_result, rules):
        rule = rules[finding.rule_id]
        cells = table.add_row().cells
        values = [
            rule.requirement,
            str(rule.category),
            str(rule.priority),
            str(finding.result),
            finding.summary,
        ]
        for index, value in enumerate(values):
            cells[index].text = value


def _render_report(
    result: AuditResult,
    file_result: FileAuditResult,
):
    rules = {rule.rule_id: rule for rule in result.rules}
    document = Document()
    apply_document_styles(document)
    document.add_heading("电力智能招标审核报告", level=0)
    document.add_paragraph(f"审核任务：{result.task_id}")
    document.add_paragraph(f"投标文件：{file_result.file.file_name}")
    document.add_paragraph(f"总体结论：{file_result.overall_conclusion}")

    if file_result.document_summary is not None:
        document.add_heading("文件摘要", level=1)
        document.add_paragraph(file_result.document_summary.summary)

    document.add_heading("项目适用性判断", level=1)
    document.add_paragraph(
        f"状态：{file_result.project_match.status}\n"
        f"理由：{file_result.project_match.reason}"
    )
    if not file_result.is_applicable:
        document.add_paragraph(
            f"未进入自动规则审核：{file_result.exclusion_reason or '原因未记录'}"
        )
        return document

    manual_materials = [
        item
        for item in result.evidence_classifications
        if str(item.classification_status) == "manual_review"
    ]
    if manual_materials:
        document.add_heading("需人工核验的投标方资料", level=1)
        for item in manual_materials:
            document.add_paragraph(
                f"{item.file_name}（{item.evidence_type}）：{item.reason}；"
                f"{item.bid_summary}"
            )

    if file_result.audit_summary:
        document.add_heading("总体审核总结", level=1)
        document.add_paragraph(file_result.audit_summary)

    if file_result.category_summaries:
        document.add_heading("分类审核总结", level=1)
        for category, summary in file_result.category_summaries.items():
            document.add_heading(category, level=2)
            document.add_paragraph(summary)

    _add_findings(document, file_result, rules)

    document.add_heading("不合规项和风险项", level=1)
    risks = [
        item
        for item in file_result.findings
        if str(item.result) in {"non_compliant", "risk"}
    ]
    for finding in risks:
        document.add_paragraph(
            f"{rules[finding.rule_id].requirement}：{finding.summary}"
        )
    if not risks:
        document.add_paragraph("无。")

    document.add_heading("无法判定与人工复核项", level=1)
    manual = [
        item for item in file_result.findings if str(item.result) == "manual_review"
    ]
    for finding in manual:
        document.add_paragraph(
            f"{rules[finding.rule_id].requirement}：{finding.summary}"
        )
    if not manual:
        document.add_paragraph("无。")

    document.add_heading("整改与复核建议", level=1)
    for suggestion in file_result.suggestions:
        document.add_paragraph(suggestion, style="List Bullet")
    if not file_result.suggestions:
        document.add_paragraph("无。")

    document.add_heading("依据与证据索引", level=1)
    sources = collect_source_index(result, file_result)
    if not sources:
        document.add_paragraph("本次结论未引用工具召回片段。")
        return document
    source_table = document.add_table(rows=1, cols=5)
    source_table.style = "Table Grid"
    for index, value in enumerate(["类型", "文件", "路径", "章节", "片段 ID"]):
        source_table.rows[0].cells[index].text = value
    for label, source in sources:
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
