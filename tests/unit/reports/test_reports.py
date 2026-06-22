"""JSON 原子写入、Word 生成和不适用文件过滤测试。"""

from app.models.documents import DocumentRef
from app.models.results import AuditResult, FileAuditResult, ProjectMatch
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


def test_word_is_generated_only_for_applicable_files(monkeypatch, temp_settings):
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
    assert len(word_paths) == 1
    assert store.resolve_relative(word_paths[0]).exists()
