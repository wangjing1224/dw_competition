"""规则视图和当前文件的确定性渲染；不总结原文、不承担 Token 预算。"""

import re

from app.models.rules import RuleSet
from app.prompts.loader import render_prompt
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

CANDIDATE_TEMPLATE = "# 当前文件候选草稿\n\n候选简介：待填写。\n\n## 候选清单\n\n待填写。\n"


def validate_candidates(text):
    """只验证单稿结构，不推断是否遗漏条件或已经合并。"""
    pattern = re.compile(r"^#{1,6} .+$", re.MULTILINE)
    matches = list(pattern.finditer(text))
    headers = [m[0] for m in matches]
    if headers[:2] != ["# 当前文件候选草稿", "## 候选清单"] or text[:matches[0].start()].strip():
        raise ValueError("保留唯一的当前文件候选草稿、候选清单标题")
    bodies = [text[m.end():matches[i + 1].start() if i + 1 < len(matches) else len(text)].strip() for i, m in enumerate(matches)]

    def filled(value):
        return bool(value.strip()) and "待填写" not in value and "〈" not in value

    intro = re.fullmatch(r"候选简介：(.+)", bodies[0], re.DOTALL)
    if not intro or not filled(intro[1]) or re.search(r"^候选简介：", intro[1], re.MULTILINE):
        raise ValueError("候选简介须唯一且已填写，概括条件；无候选时说明实际原因")
    if len(headers) == 2:
        if bodies[1] != "无候选。":
            raise ValueError("候选清单尚未填写；确无候选时写无候选。")
        return 0
    if bodies[1]:
        raise ValueError("候选清单下直接使用候选标题，不混用占位或无候选说明")
    numbers = set()
    for header, body in zip(headers[2:], bodies[2:], strict=True):
        match = re.fullmatch(r"### 候选 ([1-9]\d*)", header)
        if not match or match[1] in numbers:
            raise ValueError("候选标题须为唯一的 ### 候选 正整数")
        numbers.add(match[1])
        fields = re.fullmatch(r"要求：(.+?)\n+候选说明：(.+)", body, re.DOTALL)
        if not fields or not all(filled(v) for v in fields.groups()) or any(
            re.search(r"^(要求|候选说明|来源文件)：", v, re.MULTILINE) for v in fields.groups()
        ):
            raise ValueError(f"{header}只填写唯一且非空的要求、候选说明")
    return len(numbers)


def candidate_status(session):
    try:
        count = validate_candidates(session.candidate_draft)
        check = f"结构校验通过，候选{count}项（不代表条件已全部提取）"
    except ValueError as exc:
        check = f"结构待修正：{exc}"
    return f"当前阶段：{session.phase}\n{check}\n未解决编辑错误：{session.candidate_error or '无'}\n正式提交错误：{session.unresolved_write_error or '无'}"


def rule_system(session):
    stage = render_prompt("rule_candidate_stage" if session.phase == "extract" else "rule_merge_stage")
    return render_prompt("rule_extraction", stage_instructions=stage)


def rule_working_context(session):
    value = candidate_status(session) + "\n\n" + session.candidate_draft
    if session.phase == "merge":
        value += "\n\n## 已有规则与项目简介\n\n" + build_rule_view(session.rule_set)
    return value


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
