"""规则优先级、核验深度和总量限制的确定性策略。"""

from app.models.common import RulePriority, VerificationLevel
from app.models.rules import RuleItem


PRIORITY_ORDER = {
    RulePriority.CRITICAL: 0,
    RulePriority.IMPORTANT: 1,
    RulePriority.NORMAL: 2,
}


def normalize_rule_policy(rule: RuleItem) -> RuleItem:
    """校正模型容易输出的不合理策略组合，不修改规则业务事实。"""

    updates: dict[str, object] = {}
    if str(rule.severity) == "fatal":
        updates["priority"] = RulePriority.CRITICAL
    if rule.evidence_required:
        updates["verification_level"] = VerificationLevel.EVIDENCE_CHECK
    elif str(rule.category) == "consistency_risk":
        updates["verification_level"] = VerificationLevel.CROSS_CHECK
    elif rule.check_type in {"presence", "parameter_compare"}:
        updates["verification_level"] = VerificationLevel.BID_ONLY
    return rule.model_copy(update=updates)


def limit_rules(rules: list[RuleItem], limit: int) -> list[RuleItem]:
    """只设置总量上限；同优先级保持原始抽取顺序。"""

    ranked = sorted(
        enumerate(rules),
        key=lambda item: (
            PRIORITY_ORDER[RulePriority(str(item[1].priority))],
            item[0],
        ),
    )
    return [rule for _, rule in ranked[:limit]]
