"""规则视图和当前文件的确定性渲染；不总结原文、不承担 Token 预算。"""

from app.models.rules import RuleSet
from app.parsers.reading import (  # noqa: F401 - 保留规则侧已有导入入口。
    DocumentUnit,
    build_document_context,
    build_document_units,
    render_document_range,
)

CATEGORY_LABELS = {
    "format": "格式与文件组成",
    "qualification": "主体资格与准入",
    "commercial": "商务响应",
    "technical": "技术响应",
    "performance_capacity": "业绩与能力",
    "consistency_risk": "一致性与风险",
}
PRIORITY_LABELS = {"critical": "关键", "important": "重要", "normal": "普通"}
VERIFICATION_LABELS = {"bid_only": "投标文件内核验", "evidence_check": "投标方资料核验"}


def build_rule_view(
    rule_set: RuleSet, *, rule_ids: set[str] | None = None, include_profile: bool = True
) -> str:
    parts = []
    if include_profile:
        parts.append(
            "## 项目简介\n\n" + (rule_set.project_profile or "项目简介尚未生成")
        )
    for category, label in CATEGORY_LABELS.items():
        selected = [
            rule
            for rule in rule_set.rules
            if rule.category == category
            and (rule_ids is None or rule.rule_id in rule_ids)
        ]
        if not selected:
            continue
        parts.append(f"## {label}")
        for rule in selected:
            parts.append(
                f"### {rule.rule_id}\n\n"
                f"- 重要程度：{PRIORITY_LABELS[rule.priority]}\n"
                f"- 核验方式：{VERIFICATION_LABELS[rule.verification]}\n"
                f"- 规则要求：\n{rule.requirement}\n"
                f"- 来源文件：{'、'.join(source.file_id for source in rule.sources)}"
                + (f"\n- 依据说明：{rule.reason}" if rule.reason else "")
            )
    if rule_ids is None and not rule_set.rules:
        parts.append("当前暂无规则")
    return "\n\n".join(parts)
