"""执行模型返回的 add/merge，并由代码绑定可信来源。"""

from app.models.documents import SourceRef
from app.models.rules import (
    RuleDeltaOperation,
    RuleItem,
    RuleSet,
)
from app.rules.policies import (
    max_priority,
    max_verification,
    normalize_priority_verification,
    normalize_requirement,
    normalize_rule_policy,
    passes_rule_admission,
    stable_sort_rule_set,
    validate_rule_set,
)
from app.storage.ids import next_id


def _merge_sources(existing: list[SourceRef], incoming: SourceRef) -> list[SourceRef]:
    keys = {
        (item.file_id, item.chunk_id, item.section, item.quote)
        for item in existing
    }
    if (
        incoming.file_id,
        incoming.chunk_id,
        incoming.section,
        incoming.quote,
    ) not in keys:
        return [*existing, incoming]
    return existing


def _merge_evidence_types(current: list, incoming: list) -> list:
    return list(dict.fromkeys([*current, *incoming]))


def apply_rule_delta_operations(
    rule_set: RuleSet,
    operations: list[RuleDeltaOperation],
    *,
    source: SourceRef,
) -> RuleSet:
    """顺序执行单批增量；无效候选不污染已有规则。"""

    rules = list(rule_set.rules)
    by_id = {rule.rule_id: rule for rule in rules}
    for operation in operations:
        if operation.action == "add":
            if not passes_rule_admission(operation):
                continue
            priority, verification = normalize_priority_verification(
                operation.priority,
                operation.verification,
            )
            rule = RuleItem(
                rule_id=next_id("rule"),
                category=operation.category,
                priority=priority,
                verification=verification,
                requirement=normalize_requirement(operation.requirement or ""),
                evidence_types=operation.evidence_types,
                source=source,
            )
            rules.append(rule)
            by_id[rule.rule_id] = rule
            continue

        current = by_id.get(operation.rule_id or "")
        if current is None:
            raise ValueError(f"merge 引用了不存在的规则: {operation.rule_id}")
        absorbed = []
        for merged_rule_id in operation.merged_rule_ids:
            candidate = by_id.get(merged_rule_id)
            if candidate is None:
                raise ValueError(f"merge 引用了不存在的被合并规则: {merged_rule_id}")
            absorbed.append(candidate)
        merged_sources = list(current.supporting_sources)
        merged_evidence_types = list(current.evidence_types)
        merged_priority = current.priority
        merged_verification = current.verification
        for candidate in absorbed:
            merged_sources = _merge_sources(merged_sources, candidate.source)
            for supporting in candidate.supporting_sources:
                merged_sources = _merge_sources(merged_sources, supporting)
            merged_evidence_types = _merge_evidence_types(
                merged_evidence_types,
                candidate.evidence_types,
            )
            merged_priority = max_priority(merged_priority, candidate.priority)
            merged_verification = max_verification(
                merged_verification,
                candidate.verification,
            )
        merged = normalize_rule_policy(
            RuleItem.model_validate(
                {
                    **current.model_dump(),
                    "requirement": (
                        normalize_requirement(operation.requirement)
                        if operation.requirement
                        else current.requirement
                    ),
                    "priority": max_priority(
                        merged_priority,
                        operation.priority,
                    ),
                    "verification": max_verification(
                        merged_verification,
                        operation.verification,
                    ),
                    "evidence_types": _merge_evidence_types(
                        merged_evidence_types,
                        operation.evidence_types,
                    ),
                    "supporting_sources": _merge_sources(
                        merged_sources,
                        source,
                    ),
                }
            )
        )
        rules[rules.index(current)] = merged
        by_id[merged.rule_id] = merged
        for candidate in absorbed:
            rules.remove(candidate)
            by_id.pop(candidate.rule_id, None)
    return rule_set.model_copy(update={"rules": rules})


def finalize_rule_set(rule_set: RuleSet, *, maximum: int = 30) -> RuleSet:
    normalized = rule_set.model_copy(
        update={"rules": [normalize_rule_policy(rule) for rule in rule_set.rules]}
    )
    validate_rule_set(normalized, maximum=maximum)
    return stable_sort_rule_set(normalized)


def merge_rule_set(rule_set: RuleSet, *, limit: int = 30) -> RuleSet:
    """兼容服务层旧入口；增量抽取已经完成合并，这里只做最终校验。"""

    return finalize_rule_set(rule_set, maximum=limit)
