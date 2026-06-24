"""规则准入、重要程度与核验方式标准化、稳定排序策略。"""

import re

from app.models.common import RuleCategory, RulePriority, VerificationLevel
from app.models.rules import RuleDeltaOperation, RuleItem, RuleSet


CATEGORY_ORDER = {category: index for index, category in enumerate(RuleCategory)}
PRIORITY_ORDER = {
    RulePriority.CRITICAL: 0,
    RulePriority.IMPORTANT: 1,
    RulePriority.NORMAL: 2,
}
VERIFICATION_ORDER = {
    VerificationLevel.BID_ONLY: 0,
    VerificationLevel.SOURCE_CHECK: 1,
    VerificationLevel.EVIDENCE_CHECK: 2,
    VerificationLevel.CROSS_CHECK: 3,
}

# 这些事项发生在投标审核之后，或只能由外部平台/评标流程确认。
DISALLOWED_PATTERNS = (
    r"合同签订后",
    r"履约保证金",
    r"迟延交货",
    r"违约金",
    r"合同解除",
    r"结算方式",
    r"开标异议",
    r"评标澄清",
    r"评标排序",
    r"授标",
    r"中标通知",
    r"平台上传",
    r"系统加密",
    r"实时信用平台",
)


def normalize_requirement(value: str) -> str:
    """压缩空白；归并后的规则必须保留全部原始审核要求。"""

    return re.sub(r"\s+", " ", value).strip(" ；;。")


def passes_rule_admission(operation: RuleDeltaOperation) -> bool:
    """执行最小确定性兜底；语义准入主要由 Prompt 负责。"""

    requirement = normalize_requirement(operation.requirement or "")
    if len(requirement) < 8:
        return False
    return not any(re.search(pattern, requirement) for pattern in DISALLOWED_PATTERNS)


def normalize_priority_verification(
    priority: RulePriority,
    verification: VerificationLevel,
) -> tuple[RulePriority, VerificationLevel]:
    """资料核验或交叉核验不能仍标为普通规则。"""

    if (
        priority == RulePriority.NORMAL
        and verification
        in {VerificationLevel.EVIDENCE_CHECK, VerificationLevel.CROSS_CHECK}
    ):
        return RulePriority.IMPORTANT, verification
    return priority, verification


def normalize_rule_policy(rule: RuleItem) -> RuleItem:
    priority, verification = normalize_priority_verification(
        RulePriority(str(rule.priority)),
        VerificationLevel(str(rule.verification)),
    )
    return rule.model_copy(
        update={"priority": priority, "verification": verification}
    )


def max_priority(
    current: RulePriority,
    incoming: RulePriority | None,
) -> RulePriority:
    if incoming is None:
        return current
    return min(
        (RulePriority(str(current)), RulePriority(str(incoming))),
        key=PRIORITY_ORDER.__getitem__,
    )


def max_verification(
    current: VerificationLevel,
    incoming: VerificationLevel | None,
) -> VerificationLevel:
    if incoming is None:
        return current
    return max(
        (VerificationLevel(str(current)), VerificationLevel(str(incoming))),
        key=VERIFICATION_ORDER.__getitem__,
    )


def stable_sort_rule_set(rule_set: RuleSet) -> RuleSet:
    rules = sorted(
        rule_set.rules,
        key=lambda rule: (
            CATEGORY_ORDER[RuleCategory(str(rule.category))],
            PRIORITY_ORDER[RulePriority(str(rule.priority))],
            _numeric_rule_id(rule.rule_id),
        ),
    )
    return rule_set.model_copy(update={"rules": rules})


def _numeric_rule_id(rule_id: str) -> tuple[int, str]:
    matched = re.search(r"(\d+)$", rule_id)
    return (int(matched.group(1)) if matched else 10**9, rule_id)


def validate_rule_set(rule_set: RuleSet, *, maximum: int) -> None:
    ids = [rule.rule_id for rule in rule_set.rules]
    if len(ids) != len(set(ids)):
        raise ValueError("RuleSet 存在重复 rule_id")
    if len(ids) > maximum:
        raise ValueError(f"规则数量仍超过上限 {maximum}")
    for rule in rule_set.rules:
        if not rule.requirement.strip():
            raise ValueError(f"规则 {rule.rule_id} requirement 为空")


def limit_rules(rules: list[RuleItem], limit: int) -> list[RuleItem]:
    """兼容旧调用：只校验，不再按文件顺序静默截断。"""

    if len(rules) > limit:
        raise ValueError(f"规则数量超过上限 {limit}，必须先执行确定性同类合并")
    return list(rules)
