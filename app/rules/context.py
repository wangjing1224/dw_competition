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

CANDIDATE_ITEM_TEMPLATE = "### 候选 1\n\n要求：待填写。\n\n候选说明：待填写。"


def validate_candidates(text):
    """只验证单稿结构，不推断是否遗漏条件或已经合并。"""
    if not text.strip():
        raise ValueError("候选稿为空，尚未建立首稿；按System格式使用edit保存后再finish")
    pattern = re.compile(r"^#{1,6} .+$", re.MULTILINE)
    matches = list(pattern.finditer(text))
    headers = [m[0] for m in matches]
    if headers[:2] != ["# 当前文件候选草稿", "## 候选清单"] or text[:matches[0].start()].strip():
        raise ValueError("保留唯一的当前文件候选草稿、候选清单标题")
    bodies = [text[m.end():matches[i + 1].start() if i + 1 < len(matches) else len(text)].strip() for i, m in enumerate(matches)]

    def check_value(value, location):
        value = value.strip()
        if not value:
            raise ValueError(f"{location}不能为空；填写实际内容")
        # 只识别候选模板的整字段占位，不把原文空栏描述或书名括号当成未填。
        if value.rstrip("。.").strip() == "待填写":
            raise ValueError(f"{location}仍为模板占位“{value}”；请替换为实际内容")

    intro = re.fullmatch(r"候选简介：(.*)", bodies[0], re.DOTALL)
    if not intro or re.search(r"^候选简介：", intro[1], re.MULTILINE):
        raise ValueError("候选简介须唯一，概括条件；无候选时说明实际原因")
    check_value(intro[1], "候选简介")
    if len(headers) == 2:
        if bodies[1] != "无候选。":
            raise ValueError("候选清单尚未填写；确无候选时写无候选。")
        return 0
    if bodies[1]:
        raise ValueError("候选清单下直接使用候选标题，不混用占位或无候选说明")
    numbers = set()
    for header, body in zip(headers[2:], bodies[2:], strict=True):
        match = re.fullmatch(r"### 候选 ([1-9]\d*)", header)
        if not match:
            raise ValueError(
                f"候选标题格式错误：{header!r}。标题只能写“### 候选 正整数”，如“### 候选 1”；"
                "编号后不能附名称，标题与要求之间也不能另起名称行。"
                "所有同类条目一起修正，保留原有要求和候选说明正文。"
                f"正确条目结构如下（待填写须替换为实际内容）：\n```markdown\n{CANDIDATE_ITEM_TEMPLATE}\n```"
            )
        if match[1] in numbers:
            raise ValueError(f"候选编号重复：{header}；每项使用不同的正整数编号")
        numbers.add(match[1])
        fields = re.fullmatch(r"要求：(.*?)\n+候选说明：(.*)", body, re.DOTALL)
        if not fields or any(
            re.search(r"^(要求|候选说明|来源文件)：", v, re.MULTILINE) for v in fields.groups()
        ):
            example = CANDIDATE_ITEM_TEMPLATE.replace("### 候选 1", header, 1)
            raise ValueError(
                f"{header}正文须按顺序只填写唯一且非空的要求、候选说明；"
                "标题后直接写要求，不另起名称行，不重复字段，也不保留待填写占位。"
                "保留已提取正文，只修正不合要求的结构。"
                f"正确条目结构如下（待填写须替换为实际内容）：\n```markdown\n{example}\n```"
            )
        for label, value in zip(("要求", "候选说明"), fields.groups(), strict=True):
            check_value(value, f"{header}的{label}")
    return len(numbers)


def candidate_status(session):
    try:
        if session.candidate_draft == "":
            check = "当前候选稿为空；首稿用edit，old_text填空字符串，格式见System。"
        else:
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
