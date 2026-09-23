"""在副本上执行一批增量；不写盘、不自动压缩，调用方成功保存后才能发布状态。"""

from dataclasses import dataclass

from app.models.rules import RuleDeltaOperation, RuleItem, RuleSet
from app.rules.policies import (
    max_priority,
    max_verification,
    normalize_rule_policy,
    stable_sort_rule_set,
)
from app.storage.ids import next_id


@dataclass
class AppliedDelta:
    rule_set: RuleSet
    changed_ids: set[str]
    absorbed_ids: dict[str, str]


def apply_rule_delta_operations(
    rule_set: RuleSet,
    operations: list[RuleDeltaOperation],
    *,
    current_file_id: str,
    allowed_file_ids: set[str],
    project_profile: str | None = None,
) -> AppliedDelta:
    """来源只取系统文件快照；merge 追加正文但 reason 永远完整覆盖。"""
    files = {source.file_id: source for source in rule_set.source_files}
    if current_file_id not in files or current_file_id not in allowed_file_ids:
        raise ValueError("当前来源文件不在授权规则库范围")
    by_id = {rule.rule_id: rule.model_copy(deep=True) for rule in rule_set.rules}
    original_ids = set(by_id)
    changed: set[str] = set()
    absorbed: dict[str, str] = {}
    for index, operation in enumerate(operations, 1):
        try:
            if operation.action == "add":
                rule = RuleItem(
                    rule_id=next_id("rule"),
                    provenance="ai_generated",
                    category=operation.category,
                    requirement=operation.requirement,
                    priority=operation.priority,
                    verification=operation.verification,
                    reason=operation.reason,
                    sources=[files[current_file_id]],
                )
                if rule.rule_id in by_id or rule.rule_id in absorbed:
                    raise ValueError("系统分配了重复规则 ID")
            else:
                if (
                    operation.rule_id not in original_ids
                    or operation.rule_id not in by_id
                ):
                    raise ValueError(f"不存在可修改的原规则 {operation.rule_id}")
                current = by_id[operation.rule_id]
                if operation.action == "replace":
                    if (
                        not set(operation.source_file_ids)
                        <= allowed_file_ids & files.keys()
                    ):
                        raise ValueError("replace 来源包含未处理、越界或不存在的文件")
                    rule = RuleItem(
                        rule_id=current.rule_id,
                        provenance="ai_generated",
                        category=operation.category,
                        requirement=operation.requirement,
                        priority=operation.priority,
                        verification=operation.verification,
                        reason=operation.reason,
                        sources=[files[key] for key in operation.source_file_ids],
                    )
                else:
                    candidates = [current]
                    for key in operation.merged_rule_ids:
                        if (
                            key not in original_ids
                            or key not in by_id
                            or key in absorbed
                        ):
                            raise ValueError(f"不存在可吸收的原规则 {key}")
                        candidates.append(by_id[key])
                    priority, verification = current.priority, current.verification
                    sources = {}
                    for candidate in candidates:
                        priority = max_priority(priority, candidate.priority)
                        verification = max_verification(
                            verification, candidate.verification
                        )
                        sources.update(
                            {source.file_id: source for source in candidate.sources}
                        )
                    sources[current_file_id] = files[current_file_id]
                    parts = [candidate.requirement for candidate in candidates]
                    if operation.requirement is not None:
                        parts.append(operation.requirement)
                    rule = RuleItem(
                        rule_id=current.rule_id,
                        provenance="ai_generated",
                        category=operation.category or current.category,
                        requirement="\n".join(parts),
                        priority=max_priority(priority, operation.priority),
                        verification=max_verification(
                            verification, operation.verification
                        ),
                        reason=operation.reason,
                        sources=list(sources.values()),
                    )
                    for key in operation.merged_rule_ids:
                        del by_id[key]
                        changed.discard(key)
                        absorbed[key] = current.rule_id
            by_id[rule.rule_id] = normalize_rule_policy(rule)
            changed.add(rule.rule_id)
        except (ValueError, KeyError) as exc:
            raise ValueError(
                f"第 {index} 项 {operation.action}（{operation.rule_id or '新规则'}）：{exc}"
            ) from exc
    # A -> B -> C 的内部记录指向最终 C；已吸收项不会从 changed 返回。
    for key, target in list(absorbed.items()):
        while target in absorbed:
            target = absorbed[target]
        absorbed[key] = target
    result = RuleSet(
        project_id=rule_set.project_id,
        project_profile=rule_set.project_profile
        if project_profile is None
        else project_profile,
        rules=list(by_id.values()),
        source_files=rule_set.source_files,
    )
    return AppliedDelta(stable_sort_rule_set(result), changed, absorbed)


def finalize_rule_set(rule_set: RuleSet) -> RuleSet:
    """只校验并稳定排序；不设置条数上限或额外模型整理。"""
    return stable_sort_rule_set(RuleSet.model_validate(rule_set.model_dump()))
