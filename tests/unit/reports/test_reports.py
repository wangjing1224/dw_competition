"""JSON 原子写入、Word 生成和中文展示测试。"""

from docx import Document

from app.models.documents import BidDocumentSummary, DocumentRef
from app.models.findings import Finding
from app.models.results import AuditResult, FileAuditResult, ProjectMatch
from app.models.rules import RuleReportItem
from app.reports.json_report import read_json_result, write_json_result
from app.reports.word_report import generate_word_reports
from app.storage.file_store import FileStore


def bid(file_id, applicable):
    document = DocumentRef(
        file_id=file_id,
        file_name=f"{file_id}.docx",
        source_path=f"{file_id}.docx",
        data_role="bid_document",
        parse_status="parsed",
        project_id="project-1",
        bidder_id="bidder-1",
        task_id="audit-1",
    )
    return FileAuditResult(
        file=document,
        is_applicable=applicable,
        project_match=ProjectMatch(
            status="matched" if applicable else "not_matched",
            reason="测试",
        ),
        exclusion_reason=None if applicable else "不匹配",
        overall_conclusion="manual_review" if applicable else "not_applicable",
    )


def test_word_is_generated_for_every_file_result(monkeypatch, temp_settings):
    monkeypatch.setattr("app.storage.file_store.get_settings", lambda: temp_settings)
    store = FileStore()
    result = AuditResult(
        task_id="audit-1",
        project_id="project-1",
        bidder_id="bidder-1",
        file_results=[bid("applicable", True), bid("ignored", False)],
    )

    json_path = write_json_result("audit-1", result, store)
    word_paths = generate_word_reports(json_path, store)

    assert read_json_result(json_path, store) == result
    assert len(word_paths) == 2
    assert all(store.resolve_relative(path).exists() for path in word_paths)


def test_word_renders_markdown_and_enums_as_chinese(monkeypatch, temp_settings):
    monkeypatch.setattr("app.storage.file_store.get_settings", lambda: temp_settings)
    store = FileStore()
    file_result = bid("applicable", True).model_copy(
        update={
            "document_summary": BidDocumentSummary(
                file_id="applicable",
                match_status="matched",
                match_reason="项目一致",
                summary=(
                    "## 文件基本信息\n"
                    "- **投标方**：某某科技有限公司\n"
                    "- 已提交技术响应文件"
                ),
            ),
            "overall_conclusion": "non_compliant",
            "audit_summary": "报价信息未填写。",
            "category_summaries": {"commercial": "商务响应存在缺失。"},
            "findings": [
                Finding(
                    rule_id="rule-1",
                    result="non_compliant",
                    summary="投标报价为空。",
                )
            ],
        }
    )
    result = AuditResult(
        task_id="audit-1",
        project_id="project-1",
        bidder_id="bidder-1",
        rules=[
            RuleReportItem(
                rule_id="rule-1",
                category="commercial",
                requirement="投标函应填写明确报价",
                priority="critical",
                verification="bid_only",
                reason="报价要求",
                source_file_ids=["rule-file"],
            )
        ],
        file_results=[file_result],
    )
    json_path = write_json_result("audit-1", result, store)
    word_path = store.resolve_relative(generate_word_reports(json_path, store)[0])
    document = Document(word_path)
    text = "\n".join(
        [
            *(paragraph.text for paragraph in document.paragraphs),
            *(
                cell.text
                for table in document.tables
                for row in table.rows
                for cell in row.cells
            ),
        ]
    )
    assert "文件基本信息" in text
    assert "投标方：某某科技有限公司" in text
    assert "商务响应" in text
    assert "不符合" in text
    assert "##" not in text
    assert "**" not in text
    assert "non_compliant" not in text
    assert "commercial" not in text
    assert "critical" not in text
