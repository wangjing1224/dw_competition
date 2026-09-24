"""规则视图和当前文件的确定性渲染；不总结原文、不承担 Token 预算。"""

from dataclasses import dataclass

from app.models.documents import ParsedDocument
from app.models.rules import RuleSet
from app.parsers.render import render_block

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


@dataclass(frozen=True)
class DocumentUnit:
    section_index: int
    block_index: int


def build_document_units(parsed: ParsedDocument) -> list[DocumentUnit]:
    """顺序位置绑定调用方的文件/解析快照；不生成另一套 chunk。"""
    if parsed.schema_version != 2:
        raise ValueError(f"{parsed.document.file_id} 缺少有序解析结构，请重新解析")
    units = [
        DocumentUnit(section_index, block_index)
        for section_index, section in enumerate(parsed.sections)
        for block_index, _ in enumerate(section.blocks)
    ]
    if not units or not any(
        block.content.strip() or block.images
        for section in parsed.sections
        for block in section.blocks
    ):
        raise ValueError(f"{parsed.document.file_id} 没有可阅读的正文、表格或图片")
    return units


def render_document_range(parsed, units, start, end) -> str:
    """表格、图片及已有转写整体渲染；从章节中间开始也重新显示标题。"""
    parts = []
    previous_section = None
    for unit in units[start:end]:
        section = parsed.sections[unit.section_index]
        if previous_section != unit.section_index:
            parts.append(f"## {section.title}")
            previous_section = unit.section_index
        block = section.blocks[unit.block_index]
        pages = sorted({source.page_number for source in block.page_sources})
        if pages:
            parts.append("页码：" + "、".join(map(str, pages)))
        parts.append(render_block(block))
    if parsed.warnings:
        parts.append("## 解析限制\n" + "\n".join(parsed.warnings))
    return "\n\n".join(parts)


def build_document_context(parsed: ParsedDocument) -> str:
    """完整投影仍可复用，Agent 装载范围由控制器选择。"""
    units = build_document_units(parsed)
    return render_document_range(parsed, units, 0, len(units))
