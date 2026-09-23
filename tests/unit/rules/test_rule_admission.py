"""增量操作不静默删规则，来源/ID/等级/依据说明的约定可重复验证。"""

import pytest

from app.models.rules import RuleDeltaOperation, RuleSet, RuleSourceFile
from app.rules.merger import apply_rule_delta_operations, finalize_rule_set
from app.storage.database import initialize_database


@pytest.fixture
def rule_environment(monkeypatch, temp_settings):
    monkeypatch.setattr(
        "app.storage.database.database_path",
        lambda: temp_settings.storage.database_path,
    )
    initialize_database(temp_settings.storage.database_path)
    sources = [
        RuleSourceFile(file_id=f"file-{n}", file_name=f"{n}.md", source_path=f"{n}.md")
        for n in range(1, 4)
    ]
    return RuleSet(project_id="project-1", source_files=sources)


def add(requirement="有效期至少90日。", **overrides):
    return RuleDeltaOperation(
        action="add",
        category="commercial",
        requirement=requirement,
        priority=overrides.get("priority", "important"),
        verification=overrides.get("verification", "bid_only"),
        reason="期限依据当前文件。",
    )


def apply(rules, operations, current="file-1", profile=None):
    return apply_rule_delta_operations(
        rules,
        operations,
        current_file_id=current,
        allowed_file_ids={"file-1", "file-2"},
        project_profile=profile,
    )


def test_merge_appends_requirement_unions_sources_overwrites_reason(rule_environment):
    first = apply(rule_environment, [add()]).rule_set
    original = first.model_dump()
    rule_id = first.rules[0].rule_id
    result = apply(
        first,
        [
            RuleDeltaOperation(
                action="merge",
                rule_id=rule_id,
                requirement="自投标截止日起算。",
                priority="normal",
                verification="evidence_check",
                reason="90日依据file-1；起算时间依据file-2。",
            )
        ],
        "file-2",
    ).rule_set
    rule = result.rules[0]
    assert rule.rule_id == rule_id
    assert rule.requirement == "有效期至少90日。\n自投标截止日起算。"
    assert rule.reason == "90日依据file-1；起算时间依据file-2。"
    assert [source.file_id for source in rule.sources] == ["file-1", "file-2"]
    assert rule.priority == "important" and rule.verification == "evidence_check"
    assert first.model_dump() == original
    result = apply(
        result,
        [RuleDeltaOperation(action="merge", rule_id=rule_id, reason="")],
        "file-2",
    ).rule_set
    assert result.rules[0].requirement == rule.requirement
    assert result.rules[0].reason == ""
    assert len(result.rules[0].sources) == 2


def test_explicit_absorption_preserves_all_text_sources_and_final_changed_ids(
    rule_environment,
):
    first = apply(rule_environment, [add("条件A")]).rule_set
    second = apply(
        first,
        [add("条件B", priority="critical", verification="evidence_check")],
        "file-2",
    ).rule_set
    ids = {rule.requirement: rule.rule_id for rule in second.rules}
    result = apply(
        second,
        [
            RuleDeltaOperation(
                action="merge",
                rule_id=ids["条件A"],
                merged_rule_ids=[ids["条件B"]],
                requirement="补充C",
                reason="A、B、C依据说明",
            )
        ],
    )
    assert len(result.rule_set.rules) == 1
    rule = result.rule_set.rules[0]
    assert rule.requirement == "条件A\n条件B\n补充C"
    assert rule.priority == "critical" and rule.verification == "evidence_check"
    assert {source.file_id for source in rule.sources} == {"file-1", "file-2"}
    assert result.changed_ids == {ids["条件A"]}
    assert result.absorbed_ids == {ids["条件B"]: ids["条件A"]}


def test_replace_sources_grade_and_system_provenance(rule_environment):
    first = apply(rule_environment, [add()]).rule_set
    first.rules[0].provenance = "human_modified"
    op = RuleDeltaOperation(
        action="replace",
        rule_id=first.rules[0].rule_id,
        category="commercial",
        requirement="更正为60日。",
        priority="normal",
        verification="bid_only",
        reason="file-2更正了原90日要求。",
        source_file_ids=["file-2"],
    )
    result = apply(first, [op], "file-2").rule_set.rules[0]
    assert result.requirement == "更正为60日。" and result.priority == "normal"
    assert [source.file_id for source in result.sources] == ["file-2"]
    assert result.provenance == "ai_generated"
    with pytest.raises(ValueError, match="越界"):
        apply(first, [op.model_copy(update={"source_file_ids": ["file-3"]})], "file-2")


def test_failed_batch_does_not_mutate_old_rules_or_reuse_allocated_id(rule_environment):
    before = rule_environment.model_dump()
    with pytest.raises(ValueError, match="不存在"):
        apply(
            rule_environment,
            [add(), RuleDeltaOperation(action="merge", rule_id="missing", reason="")],
            profile="不能提前保存的简介",
        )
    assert rule_environment.model_dump() == before
    next_result = apply(rule_environment, [add()]).rule_set
    assert next_result.rules[0].rule_id == "rule-2"


def test_no_count_limit_or_automatic_group_compression(rule_environment):
    result = apply(rule_environment, [add(f"独立条件{n}") for n in range(35)]).rule_set
    assert len(finalize_rule_set(result).rules) == 35


def test_normal_evidence_check_is_raised_to_important(rule_environment):
    rule = apply(
        rule_environment, [add(priority="normal", verification="evidence_check")]
    ).rule_set.rules[0]
    assert rule.priority == "important"
