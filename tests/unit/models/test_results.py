"""AuditResult 归属和文件覆盖校验测试。"""

import pytest
from pydantic import ValidationError

from app.models.documents import DocumentRef
from app.models.results import AuditResult, FileAuditResult, ProjectMatch


def test_duplicate_file_result_is_rejected():
    document = DocumentRef(
        file_id="file-1",
        file_name="投标文件.docx",
        source_path="投标文件.docx",
        data_role="bid_document",
        parse_status="parsed",
        project_id="project-1",
        bidder_id="bidder-1",
        task_id="audit-1",
    )
    file_result = FileAuditResult(
        file=document,
        is_applicable=False,
        project_match=ProjectMatch(
            status="not_matched",
            reason="物资类别不匹配",
        ),
        exclusion_reason="物资类别不匹配",
        overall_conclusion="not_applicable",
    )

    with pytest.raises(ValidationError):
        AuditResult(
            task_id="audit-1",
            project_id="project-1",
            bidder_id="bidder-1",
            file_results=[file_result, file_result],
        )
