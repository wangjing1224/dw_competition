"""执行模型返回的 add/merge，并由代码绑定可信来源。"""

from itertools import combinations

from app.models.common import RulePriority, VerificationLevel
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
    PRIORITY_ORDER,
    stable_sort_rule_set,
    validate_rule_set,
    VERIFICATION_ORDER,
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


def _all_sources(rule: RuleItem) -> list[SourceRef]:
    return [rule.source, *rule.supporting_sources]


def _merge_requirements(left: str, right: str) -> str:
    """按原始审核点拼接，避免粗合并时丢失任一规则正文。"""

    items = [
        item.strip(" ；;。")
        for requirement in (left, right)
        for item in requirement.split("；")
        if item.strip(" ；;。")
    ]
    return normalize_requirement("；".join(dict.fromkeys(items)))


def _keeper_and_absorbed(
    left: RuleItem,
    right: RuleItem,
) -> tuple[RuleItem, RuleItem]:
    """高重要程度优先；同级时保留更深核验方式，再保留较小 ID。"""

    def keeper_key(rule: RuleItem) -> tuple[int, int, tuple[int, str]]:
        return (
            PRIORITY_ORDER[RulePriority(str(rule.priority))],
            -VERIFICATION_ORDER[VerificationLevel(str(rule.verification))],
            _rule_id_key(rule.rule_id),
        )

    return (
        (left, right)
        if keeper_key(left) <= keeper_key(right)
        else (right, left)
    )


def _rule_id_key(rule_id: str) -> tuple[int, str]:
    suffix = rule_id.rsplit("-", 1)[-1]
    return (int(suffix) if suffix.isdigit() else 10**9, rule_id)


def _merge_rule_pair(keeper: RuleItem, absorbed: RuleItem) -> RuleItem:
    supporting = list(keeper.supporting_sources)
    for source in _all_sources(absorbed):
        supporting = _merge_sources(supporting, source)
    return normalize_rule_policy(
        RuleItem.model_validate(
            {
                **keeper.model_dump(),
                "requirement": _merge_requirements(
                    keeper.requirement,
                    absorbed.requirement,
                ),
                "priority": max_priority(
                    keeper.priority,
                    absorbed.priority,
                ),
                "verification": max_verification(
                    keeper.verification,
                    absorbed.verification,
                ),
                "evidence_types": _merge_evidence_types(
                    keeper.evidence_types,
                    absorbed.evidence_types,
                ),
                "supporting_sources": supporting,
            }
        )
    )


def _merge_group(group: list[RuleItem]) -> RuleItem:
    """把同一归并组完整合成一条，所有来源映射逐条迁移。"""

    keeper = min(
        group,
        key=lambda rule: (
            PRIORITY_ORDER[RulePriority(str(rule.priority))],
            -VERIFICATION_ORDER[
                VerificationLevel(str(rule.verification))
            ],
            _rule_id_key(rule.rule_id),
        ),
    )
    merged = keeper
    for absorbed in sorted(
        (rule for rule in group if rule.rule_id != keeper.rule_id),
        key=lambda rule: _rule_id_key(rule.rule_id),
    ):
        merged = _merge_rule_pair(merged, absorbed)
    return merged


def _merge_all_groups(
    rules: list[RuleItem],
    *,
    key_fields: tuple[str, ...],
) -> list[RuleItem]:
    """完成当前层级的全部可合并组，不在达到30条时中途停止。"""

    groups: dict[tuple[str, ...], list[RuleItem]] = {}
    for rule in rules:
        key = tuple(str(getattr(rule, field)) for field in key_fields)
        groups.setdefault(key, []).append(rule)
    merged_by_id: dict[str, RuleItem] = {}
    absorbed_ids: set[str] = set()
    for group in groups.values():
        if len(group) < 2:
            continue
        merged = _merge_group(group)
        merged_by_id[merged.rule_id] = merged
        absorbed_ids.update(
            rule.rule_id for rule in group if rule.rule_id != merged.rule_id
        )
    return [
        merged_by_id.get(rule.rule_id, rule)
        for rule in rules
        if rule.rule_id not in absorbed_ids
    ]


def compact_rule_set(rule_set: RuleSet, *, maximum: int) -> RuleSet:
    """分层完成确定性同类归并，不按超出数量机械停止。"""

    rules = list(rule_set.rules)
    if len(rules) <= maximum:
        return rule_set

    merge_levels = (
        ("category", "priority", "verification"),
        ("category", "priority"),
        ("category", "verification"),
        ("category",),
    )
    for key_fields in merge_levels:
        rules = _merge_all_groups(rules, key_fields=key_fields)
        if len(rules) <= maximum:
            break
    if len(rules) > maximum:
        raise ValueError(
            f"完成全部同类归并后仍有 {len(rules)} 条规则，超过上限 {maximum}"
        )
    return rule_set.model_copy(update={"rules": rules})


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
