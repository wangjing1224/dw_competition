"""规则增量 Schema 和章节组批测试。"""

import pytest
from pydantic import ValidationError

from app.models.documents import DocumentRef, ParsedDocument, ParsedSection
from app.models.rules import RuleDeltaResult
from app.rules.extractor import build_rule_extraction_batches


def test_rule_delta_accepts_empty_operations():
    result = RuleDeltaResult.model_validate(
        {"project_profile": {"project_name": "示例项目"}, "operations": []}
    )
    assert result.project_profile.project_name == "示例项目"
    assert result.operations == []


def test_add_operation_requires_core_fields():
    with pytest.raises(ValidationError):
        RuleDeltaResult.model_validate(
            {
                "project_profile": {},
                "operations": [{"action": "add", "reason": "缺少字段"}],
            }
        )


def test_long_parsed_document_is_split_into_bounded_batches():
    parsed = ParsedDocument(
        document=DocumentRef(
            file_id="file-1",
            file_name="规则.md",
            source_path="招标文件/规则.md",
            data_role="rule_source",
            parse_status="parsed",
            project_id="project-1",
        ),
        parse_status="parsed",
        sections=[
            ParsedSection(
                section_id="section-1",
                title="技术要求",
                content="像素不低于640x480。" * 100,
            )
        ],
    )
    batches = build_rule_extraction_batches(
        parsed,
        max_chars=300,
        overlap_chars=30,
    )
    assert len(batches) > 1
    assert all(len(batch) <= 300 for batch in batches)
