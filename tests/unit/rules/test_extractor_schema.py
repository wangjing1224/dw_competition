"""规则抽取批次对常见模型结构偏差的兼容测试。"""

import pytest
from pydantic import ValidationError

from app.models.documents import DocumentRef, ParsedDocument, ParsedSection
from app.rules.extractor import (
    RuleExtractionBatch,
    build_rule_extraction_batches,
)


def test_flattened_project_profile_is_moved_under_profile():
    batch = RuleExtractionBatch.model_validate(
        {
            "project_name": "示例项目",
            "buyer": "示例采购人",
            "material_category": "物资类",
            "bid_deadline": "2026-04-21 09:30",
            "bid_validity_days": None,
            "rules": [],
        }
    )

    assert batch.project_profile.project_name == "示例项目"
    assert batch.project_profile.buyer == "示例采购人"
    assert batch.rules == []


def test_unknown_root_field_is_still_rejected():
    with pytest.raises(ValidationError):
        RuleExtractionBatch.model_validate(
            {
                "project_profile": {},
                "rules": [],
                "unexpected": "不能静默忽略",
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
