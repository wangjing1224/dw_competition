"""按稳定语义键去重规则，并把无法消解的要求标记为冲突。"""

import json
from collections import defaultdict

from app.models.rules import RuleItem, RuleSet


def _semantic_key(rule: RuleItem) -> tuple[str, str, str, str]:
    return (
        str(rule.category),
        rule.target_field or rule.name.strip().lower(),
        rule.scope.package_no or "",
        rule.scope.material_name or "",
    )


def _requirement_signature(rule: RuleItem) -> str:
    return json.dumps(
        {
            "operator": rule.operator,
            "expected": rule.expected_value,
            "requirement": " ".join(rule.requirement.split()),
        },
        ensure_ascii=False,
        sort_keys=True,
    )


def merge_rule_set(rule_set: RuleSet) -> RuleSet:
    """第一版只做可解释的精确去重，不做额外 LLM 语义合并。"""

    groups: dict[tuple[str, str, str, str], list[RuleItem]] = defaultdict(list)
    for rule in rule_set.rules:
        groups[_semantic_key(rule)].append(rule)

    merged: list[RuleItem] = []
    for group in groups.values():
        primary = group[0]
        signatures = {_requirement_signature(item) for item in group}
        supporting = [
            source
            for item in group[1:]
            for source in [item.source, *item.supporting_sources]
        ]
        if len(signatures) == 1:
            merged.append(
                primary.model_copy(
                    update={
                        "supporting_sources": [
                            *primary.supporting_sources,
                            *supporting,
                        ]
                    }
                )
            )
        else:
            merged.append(
                primary.model_copy(
                    update={
                        "status": "rule_conflict",
                        "conflict_note": "相同作用域下存在无法自动消解的不同要求",
                        "supporting_sources": [
                            *primary.supporting_sources,
                            *supporting,
                        ],
                    }
                )
            )
    return rule_set.model_copy(update={"rules": merged})
