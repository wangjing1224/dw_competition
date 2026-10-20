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


@pytest.mark.parametrize("profile", ["null", "NULL", "Null", " \nnull\t "])
def test_profile_rejects_null_text_instead_of_silently_coercing(profile):
    with pytest.raises(ValidationError) as error:
        SubmitRuleDeltaInput(
            operations=None, project_profile=profile, return_view="changed",
        )
    assert error.value.errors()[0]["loc"] == ("project_profile",)
    assert "不修改简介请传 JSON null" in str(error.value)
    assert "本次提交未写入" in str(error.value)


@pytest.mark.parametrize("profile", ["", " \n\t ", {}, [], 0, False])
def test_profile_still_rejects_blank_or_non_text_values(profile):
    with pytest.raises(ValidationError):
        SubmitRuleDeltaInput(
            operations=None, project_profile=profile, return_view="changed",
        )


@pytest.mark.parametrize("profile", ["完整新项目简介。", '项目涉及接口字段 null 的处理。'])
def test_profile_keeps_valid_text_verbatim(profile):
    request = SubmitRuleDeltaInput(
        operations=None, project_profile=profile, return_view="changed",
    )
    assert request.project_profile == profile


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


def operation_payload(action):
    payload = {"action": action, "reason": "当前原文明示的要求。"}
    if action in {"add", "replace"}:
        payload.update(
            category="commercial", requirement="期限90日。",
            priority="important", verification="bid_only",
        )
    if action != "add":
        payload["rule_id"] = "rule-1"
    if action == "replace":
        payload["source_file_ids"] = ["file-1"]
    return payload


@pytest.mark.parametrize("action,fields", [
    ("add", ["rule_id", "merged_rule_ids", "source_file_ids"]),
    ("merge", ["merged_rule_ids", "source_file_ids"]),
    ("replace", ["merged_rule_ids"]),
])
def test_inactive_null_fields_are_equivalent_to_omission(action, fields):
    minimal = operation_payload(action)
    payload = {**minimal, **dict.fromkeys(fields)}
    before = dict(payload)
    operation = RuleDeltaOperation.model_validate(payload)
    assert payload == before  # 不修改原始调用参数。
    assert not set(fields) & operation.model_fields_set
    assert operation.merged_rule_ids == []
    assert operation.model_dump(exclude_unset=True) == minimal
    # 嵌套工具参数再次校验仍保留字段是否实际传入的语义。
    request = SubmitRuleDeltaInput(
        operations=[operation], project_profile=None, return_view="changed",
    )
    assert request.model_dump(exclude_unset=True)["operations"] == [minimal]


@pytest.mark.parametrize("action,overrides", [
    ("add", {"rule_id": "rule-1"}),
    ("add", {"merged_rule_ids": []}),
    ("add", {"source_file_ids": []}),
    ("add", {"source_file_ids": ["file-1"]}),
    ("add", {"category": None}),
    ("add", {"requirement": None}),
    ("add", {"priority": None}),
    ("add", {"verification": None}),
    ("add", {"reason": None}),
    ("add", {"unknown": None}),
    ("add", {"action": []}),
    ("merge", {"rule_id": None}),
    ("merge", {"source_file_ids": []}),
    ("merge", {"source_file_ids": ["file-1"]}),
    ("merge", {"merged_rule_ids": ["rule-1"]}),
    ("merge", {"reason": None}),
    ("replace", {"merged_rule_ids": []}),
    ("replace", {"source_file_ids": None}),
    ("replace", {"source_file_ids": []}),
    ("replace", {"source_file_ids": ["file-1", "file-1"]}),
    ("replace", {"rule_id": None}),
    ("replace", {"reason": None}),
])
def test_null_compatibility_does_not_relax_real_operation_constraints(action, overrides):
    with pytest.raises(ValidationError):
        RuleDeltaOperation.model_validate({**operation_payload(action), **overrides})


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
