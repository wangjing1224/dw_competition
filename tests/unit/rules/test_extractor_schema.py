"""新规则协议只接受显式三参数；文档输入不做固定字符组批。"""

import pytest
from pydantic import ValidationError

from app.models.documents import DocumentRef, ParsedBlock, ParsedDocument, ParsedSection
from app.models.rules import (
    RuleDeltaOperation,
    RuleItem,
    RuleSet,
    RuleSourceFile,
    SubmitRuleDeltaInput,
)
from app.rules.context import build_document_context, build_rule_view


def test_read_view_requires_all_three_explicit_nulls():
    assert (
        SubmitRuleDeltaInput(
            operations=None, project_profile=None, return_view=None
        ).operations
        is None
    )
    for payload in [
        {},
        {"operations": [], "project_profile": None, "return_view": None},
        {"operations": None, "project_profile": None, "return_view": "all"},
    ]:
        with pytest.raises(ValidationError):
            SubmitRuleDeltaInput.model_validate(payload)


@pytest.mark.parametrize(
    "payload",
    [
        {"action": "add", "reason": ""},
        {"action": "merge", "rule_id": "rule-1"},
        {"action": "merge", "rule_id": "rule-1", "reason": None},
        {
            "action": "merge",
            "rule_id": "rule-1",
            "reason": "",
            "source_file_ids": ["file-1"],
        },
        {
            "action": "merge",
            "rule_id": "rule-1",
            "reason": "",
            "provenance": "human_modified",
        },
        {"action": "replace", "rule_id": "rule-1", "reason": ""},
    ],
)
def test_invalid_operations_rejected(payload):
    with pytest.raises(ValidationError):
        RuleDeltaOperation.model_validate(payload)


def test_rule_has_exact_persisted_fields_and_view_has_no_provenance():
    source = RuleSourceFile(
        file_id="file-1", file_name="规范.md", source_path="目录/规范.md"
    )
    rule = RuleItem(
        rule_id="rule-1",
        provenance="ai_generated",
        category="technical",
        requirement="温度范围不小于-20至50℃。",
        priority="critical",
        verification="bid_only",
        reason="",
        sources=[source],
    )
    rules = RuleSet(project_id="project-1", source_files=[source], rules=[rule])
    assert set(rules.model_dump()) == {
        "project_id",
        "project_profile",
        "rules",
        "source_files",
    }
    assert set(rule.model_dump()) == {
        "rule_id",
        "provenance",
        "category",
        "requirement",
        "priority",
        "verification",
        "reason",
        "sources",
    }
    view = build_rule_view(rules)
    assert "file-1" in view and "rule-1" in view and "critical" not in view
    assert "provenance" not in view and "ai_generated" not in view


def test_complete_ordered_blocks_without_duplicate_legacy_fields():
    text = "原文内容。" * 20000
    table = "|要求|阈值|\n|---|---|\n|指标|90|"
    parsed = ParsedDocument(
        schema_version=2,
        document=DocumentRef(
            file_id="file-1",
            file_name="规范.md",
            source_path="规范.md",
            data_role="rule_source",
            project_id="project-1",
        ),
        parse_status="parsed",
        sections=[
            ParsedSection(
                section_id="s",
                title="技术要求",
                content="不要重复的旧投影",
                blocks=[
                    ParsedBlock(kind="text", content=text, source_block_index=0),
                    ParsedBlock(kind="table", content=table, source_block_index=1),
                ],
            )
        ],
    )
    context = build_document_context(parsed)
    assert text in context and table in context
    assert context.index(text) < context.index(table)
    assert "不要重复的旧投影" not in context
