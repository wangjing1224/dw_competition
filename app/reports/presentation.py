"""正式阅读稿的确定性投影。只读结果与冻结来源，不检索、不调用模型。"""

import re
from zoneinfo import ZoneInfo

from app.models.results import AuditResult
from app.parsers.assets import atomic_write
from app.reports.styles import match_label, priority_label

CATEGORIES = {
    "format": "格式与文件组成",
    "qualification": "主体资格与准入",
    "commercial": "商务响应",
    "technical": "技术响应",
    "performance_capacity": "业绩与能力",
    "consistency_risk": "一致性与风险",
}
RESULTS = {
    "compliant": "符合",
    "non_compliant": "不符合",
    "manual_review": "人工复核",
    "not_applicable": "不适用",
}
SOURCE_FIELDS = (
    "tender_chunk_ids",
    "bid_chunk_ids",
    "evidence_chunk_ids",
    "image_paths",
)
ROLES = {"rule_source": "招标", "bid_document": "投标", "evidence_material": "资料"}


def literal(value):
    """系统元信息按文字呈现，不能注入标题、表格或链接。"""
    return re.sub(
        r"([\\`*_\[\]<>|#])", r"\\\1", str(value).replace("\n", " ").replace("\r", " ")
    )


def heading_anchor(title):
    """下载稿使用常见 Markdown 阅读器的标题锚点，Word 映射到原生书签。"""
    return re.sub(r"[^\w -]", "", title.lower()).replace(" ", "-")


def report_markdown(result: AuditResult, file_result) -> str:
    if file_result.report_format_version != 2:
        raise ValueError("旧版结果不自动转换为新版报告")
    parts = [f"# {literal(file_result.display_title)}", "## 一、审核概况"]
    context = file_result.report_context
    meta = [
        ("审核任务", context.task_name if context else None),
        ("审核文件", file_result.file.file_name),
        ("规则库", context.rule_library_name if context else None),
        (
            "招标依据文件",
            "、".join(dict.fromkeys(f["file_name"] for f in result.rule_source_files)),
        ),
        ("资料库", context.evidence_library_name if context else None),
        (
            "报告形成时间",
            context.finalized_at.astimezone(ZoneInfo("Asia/Shanghai")).strftime(
                "%Y年%m月%d日 %H:%M"
            )
            if context and context.finalized_at
            else None,
        ),
    ]
    parts.append(
        "| 项目 | 内容 |\n|---|---|\n"
        + "\n".join(f"| {label} | {literal(value)} |" for label, value in meta if value)
    )
    if not file_result.is_applicable:
        parts += [
            "## 二、项目适用性说明",
            f"**匹配结果：** {match_label(file_result.project_match.status)}",
            file_result.exclusion_reason or file_result.project_match.reason,
            "本文件未进入规则审核，不生成普通规则结论或统计。",
        ]
        return "\n\n".join(parts) + "\n"
    findings = {f.rule_id: f for f in file_result.findings}
    ordinals = {r.rule_id: f"4.{i + 1}" for i, r in enumerate(result.rules)}
    detail_titles = {
        r.rule_id: f"{ordinals[r.rule_id]} {CATEGORIES[r.category]}｜{RESULTS[findings[r.rule_id].result]}"
        for r in result.rules
    }
    parts += [
        f"本次按所选规则库审核，共核验{len(findings)}项要求。审核范围限于本次提交文件和已选资料库的固定版本，不覆盖未提交材料或外部真实性调查。",
        "## 二、结果总览",
        f"### 总体结论：{RESULTS[file_result.overall_conclusion]}",
        file_result.audit_summary or "",
    ]
    counts = [
        sum(f.result == key for f in findings.values())
        for key in ("compliant", "non_compliant", "manual_review")
    ]
    parts += [
        "| 已审核要求 | 符合 | 不符合 | 人工复核 |\n|---|---|---|---|\n| "
        + " | ".join(f"{n}项" for n in [len(findings), *counts])
        + " |",
        "## 三、重点问题与处理建议",
    ]
    for index, highlight in enumerate(file_result.highlights):
        selected = [
            findings[r.rule_id] for r in result.rules if r.rule_id in highlight.rule_ids
        ]
        parts += [
            f"### {index + 1}. {literal(highlight.title)}",
            "**涉及结论：** "
            + "、".join(dict.fromkeys(RESULTS[f.result] for f in selected)),
            "**问题概述**",
            highlight.summary,
        ]
        suggestions = {}
        for f in selected:
            if f.recommendation and f.recommendation.strip():
                suggestions.setdefault(f.recommendation.strip(), []).append(
                    ordinals[f.rule_id]
                )
        if suggestions:
            parts.append("**处理建议**")
            for text, sections in suggestions.items():
                parts += ["、".join(f"第{s}节" for s in sections) + "：", text]
        parts.append(
            "**查看依据：** "
            + "、".join(
                f"[第{ordinals[f.rule_id]}节](#{heading_anchor(detail_titles[f.rule_id])})"
                for f in selected
            )
        )
    if not file_result.highlights:
        parts.append("无重点问题。完整结果见第四章。")
    parts += [
        "以上为审阅建议，不代表已获准修改投标文件、补交材料或完成资格确认。",
        "## 四、审核明细及原文摘录",
    ]
    for rule in result.rules:
        f = findings[rule.rule_id]
        parts += [
            f"### {detail_titles[rule.rule_id]}",
            f"**重要程度：** {priority_label(rule.priority)}",
            "**审核要求**",
            rule.requirement,
            "**实际发现**",
            f.observation or "",
            "**判断说明**",
            f.summary,
        ]
        if f.recommendation:
            parts += ["**复核建议**", f.recommendation]
        source_names = {s["file_id"]: s["file_name"] for s in result.rule_source_files}
        names = list(
            dict.fromkeys(
                literal(source_names[key])
                for key in rule.source_file_ids
                if key in source_names
            )
        )
        if names:
            parts += ["**规则依据文件**", "、".join(names)]
        keys = dict.fromkeys(
            key for field in SOURCE_FIELDS for key in getattr(f, field)
        )
        for key in keys:
            source = file_result.sources[key]
            location = " · ".join(
                literal(v)
                for v in (
                    source.file_name,
                    source.section,
                    "页 " + "、".join(map(str, source.page_numbers))
                    if source.page_numbers
                    else None,
                )
                if v
            )
            parts += [
                f"**{ROLES[source.data_role]}{'解析原文摘录' if source.quote else '图片依据' if key in f.image_paths else '核验依据'}**",
                location,
            ]
            if source.quote:
                parts.append(
                    "\n".join("> " + line for line in source.quote.splitlines())
                )
    parts.append(
        "本报告用于辅助审阅，正式处理意见应由相关人员结合招标文件和原始材料确认。"
    )
    return "\n\n".join(parts) + "\n"


def write_formal_markdown(result, file_result, store):
    path = (
        store.task_output_dir(result.task_id)
        / "reports"
        / f"{file_result.file.file_id}.md"
    )
    atomic_write(path, report_markdown(result, file_result).encode("utf-8"))
    return path


def write_formal_reports(result, store):
    for file_result in result.file_results:
        if file_result.report_format_version == 2:
            write_formal_markdown(result, file_result, store)
