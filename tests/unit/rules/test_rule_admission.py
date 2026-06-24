"""Task16 规则准入、投影、组合修正和容量合并测试。"""

from app.models.documents import SourceRef
from app.models.rules import (
    RuleDeltaOperation,
    RuleItem,
    RuleSet,
    present_rules_for_model,
)
from app.rules.merger import apply_rule_delta_operations, compact_rule_set
from app.rules.policies import (
    normalize_priority_verification,
    passes_rule_admission,
)


def source(file_id="file-1", chunk_id=None):
    return SourceRef(
        file_id=file_id,
        chunk_id=chunk_id,
        file_name="招标文件.md",
        source_path="招标文件.md",
    )


def test_post_award_contract_term_is_rejected():
    operation = RuleDeltaOperation(
        action="add",
        category="commercial",
        requirement="合同签订后迟延交货应按日支付违约金",
        priority="important",
        verification="source_check",
        reason="合同履约条款",
    )
    assert passes_rule_admission(operation) is False


def test_evidence_check_normal_priority_is_promoted():
    priority, verification = normalize_priority_verification(
        "normal",
        "evidence_check",
    )
    assert priority == "important"
    assert verification == "evidence_check"


def test_model_projection_does_not_expose_sources():
    rule_set = RuleSet(
        project_id="project-1",
        rules=[
            RuleItem(
                rule_id="rule-1",
                category="technical",
                requirement="探测器分辨率不低于640x480",
                priority="critical",
                verification="source_check",
                source=source(),
            )
        ],
    )
    value = present_rules_for_model(rule_set)[0].model_dump()
    assert set(value) == {
        "rule_id",
        "category",
        "requirement",
        "priority",
        "verification",
        "evidence_types",
    }


def test_capacity_merge_removes_absorbed_rules_and_preserves_sources():
    first = RuleItem(
        rule_id="rule-1",
        category="technical",
        requirement="成像分辨率应满足技术规范要求",
        priority="critical",
        verification="source_check",
        source=source("file-1"),
    )
    second = RuleItem(
        rule_id="rule-2",
        category="technical",
        requirement="测温精度和热灵敏度应满足技术规范要求",
        priority="important",
        verification="source_check",
        source=source("file-2"),
    )
    merged = apply_rule_delta_operations(
        RuleSet(project_id="project-1", rules=[first, second]),
        [
            RuleDeltaOperation(
                action="merge",
                rule_id="rule-1",
                merged_rule_ids=["rule-2"],
                requirement="成像分辨率、测温精度和热灵敏度应满足技术规范要求",
                priority="critical",
                verification="source_check",
                reason="同属核心成像和测温性能要求",
            )
        ],
        source=source("file-2"),
    )
    assert [rule.rule_id for rule in merged.rules] == ["rule-1"]
    assert {item.file_id for item in merged.rules[0].supporting_sources} == {
        "file-2"
    }


def test_deterministic_capacity_merge_prefers_same_priority_and_verification():
    rules = [
        RuleItem(
            rule_id="rule-1",
            category="technical",
            requirement="成像分辨率应满足技术规范要求",
            priority="critical",
            verification="source_check",
            source=source("file-1", "chunk-1"),
        ),
        RuleItem(
            rule_id="rule-2",
            category="technical",
            requirement="测温精度应满足技术规范要求",
            priority="critical",
            verification="source_check",
            source=source("file-2", "chunk-2"),
        ),
        RuleItem(
            rule_id="rule-3",
            category="technical",
            requirement="投标人应提供产品检测报告",
            priority="important",
            verification="evidence_check",
            evidence_types=["test_report"],
            source=source("file-3", "chunk-3"),
        ),
        RuleItem(
            rule_id="rule-4",
            category="commercial",
            requirement="投标报价不得超过最高限价",
            priority="critical",
            verification="bid_only",
            source=source("file-4", "chunk-4"),
        ),
    ]
    compacted = compact_rule_set(
        RuleSet(project_id="project-1", rules=rules),
        maximum=3,
    )
    assert {rule.rule_id for rule in compacted.rules} == {
        "rule-1",
        "rule-3",
        "rule-4",
    }
    merged = next(rule for rule in compacted.rules if rule.rule_id == "rule-1")
    assert "成像分辨率" in merged.requirement
    assert "测温精度" in merged.requirement
    assert {
        item.chunk_id for item in merged.supporting_sources
    } == {"chunk-2"}


def test_deterministic_merge_keeps_high_priority_and_upgrades_verification():
    compacted = compact_rule_set(
        RuleSet(
            project_id="project-1",
            rules=[
                RuleItem(
                    rule_id="rule-8",
                    category="qualification",
                    requirement="投标主体应具有合法有效的营业执照",
                    priority="critical",
                    verification="bid_only",
                    source=source("file-8", "chunk-8"),
                ),
                RuleItem(
                    rule_id="rule-9",
                    category="qualification",
                    requirement="营业执照主体应与投标主体保持一致",
                    priority="important",
                    verification="evidence_check",
                    evidence_types=["business_license"],
                    source=source("file-9", "chunk-9"),
                ),
            ],
        ),
        maximum=1,
    )
    merged = compacted.rules[0]
    assert merged.rule_id == "rule-8"
    assert merged.priority == "critical"
    assert merged.verification == "evidence_check"
    assert merged.evidence_types == ["business_license"]
    assert {item.chunk_id for item in merged.supporting_sources} == {"chunk-9"}
