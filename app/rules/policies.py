"""确定性等级组合与稳定排序；语义准入交给规则 Agent，不按关键词静默丢弃。"""

import re

from app.models.common import RuleCategory, RulePriority, VerificationLevel
from app.models.rules import RuleItem, RuleSet

CATEGORY_ORDER = {category: index for index, category in enumerate(RuleCategory)}
PRIORITY_ORDER = {
    RulePriority.CRITICAL: 0,
    RulePriority.IMPORTANT: 1,
    RulePriority.NORMAL: 2,
}


def max_priority(current, incoming):
    return (
        current
        if incoming is None
        else min((current, incoming), key=PRIORITY_ORDER.__getitem__)
    )


def max_verification(current, incoming):
    return (
        VerificationLevel.EVIDENCE_CHECK
        if VerificationLevel.EVIDENCE_CHECK in (current, incoming)
        else VerificationLevel.BID_ONLY
    )


def normalize_priority_verification(priority, verification):
    if (
        priority == RulePriority.NORMAL
        and verification == VerificationLevel.EVIDENCE_CHECK
    ):
        return RulePriority.IMPORTANT, verification
    return priority, verification


def normalize_rule_policy(rule: RuleItem) -> RuleItem:
    priority, verification = normalize_priority_verification(
        rule.priority, rule.verification
    )
    return rule.model_copy(update={"priority": priority, "verification": verification})


def stable_sort_rule_set(rule_set: RuleSet) -> RuleSet:
    def key(rule):
        suffix = re.search(r"(\d+)$", rule.rule_id)
        return (
            CATEGORY_ORDER[rule.category],
            PRIORITY_ORDER[rule.priority],
            int(suffix[1]) if suffix else 10**9,
            rule.rule_id,
        )

    return rule_set.model_copy(update={"rules": sorted(rule_set.rules, key=key)})
