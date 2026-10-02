"""新版内部草稿契约；旧任务继续使用 v1，不根据正文猜测版本。"""

import re

from app.models.findings import ReportHighlight
from app.models.tasks import (
    AgentAuditDecision,
    CategoryAuditDecision,
    RuleAuditDecision,
)
from app.reports.markdown_report import (
    PENDING,
    RESULTS,
    SOURCE_FIELDS,
    TITLE_PLACEHOLDER,
    report_groups,
    skeleton,
)


def fields(body, allowed, required):
    matches = list(re.finditer(r"^(" + "|".join(allowed) + r")：", body, re.MULTILINE))
    labels = [m[1] for m in matches]
    if (
        labels[: len(required)] != required
        or len(labels) != len(set(labels))
        or not matches
        or matches[0].start() != 0
    ):
        raise ValueError("字段须唯一且按约定顺序填写：" + "、".join(required))
    values = {
        m[1]: body[
            m.end() : matches[i + 1].start() if i + 1 < len(matches) else len(body)
        ].strip()
        for i, m in enumerate(matches)
    }
    if any(not value for value in values.values()):
        raise ValueError("已填写字段不能为空；可选字段无值请省略整行")
    return values


def parse_v2(text, rule_set, ledger, *, final=False):
    headings = list(re.finditer(r"^#{1,6} .+$", text, re.MULTILINE))
    headers = [m[0] for m in headings]
    ordinary = [h for h in headers if not re.match(r"^### 问题 \d+：", h)]
    expected = re.findall(r"^#{1,6} .+$", skeleton(rule_set, 2), re.MULTILINE)
    issue_headers = [h for h in headers if re.match(r"^### 问题 \d+：", h)]
    if (
        not headers
        or not headers[0].startswith("# ")
        or ordinary[1:] != expected[1:]
        or headers[1:] != expected[1:3] + issue_headers + expected[3:]
        or text[: headings[0].start()].strip()
    ):
        raise ValueError("报告须有一个一级标题，类别或规则集合/顺序须与当前骨架一致")
    sections = [
        (
            m[0],
            text[
                m.end() : headings[i + 1].start()
                if i + 1 < len(headings)
                else len(text)
            ].strip(),
        )
        for i, m in enumerate(headings)
    ]
    missing = []

    def content(body, label):
        if not body or body == PENDING:
            missing.append(label)
            return ""
        if PENDING in body or "〈" in body or "```" in body:
            raise ValueError(f"{label}仍包含模板占位或代码围栏")
        return body

    title = headers[0][2:].strip()
    if title in {TITLE_PLACEHOLDER, PENDING}:
        missing.append("报告标题")
    elif not title or len(title) > 120 or re.search(r"[〈`*\[\]<>]", title):
        raise ValueError("报告标题须为1至120字的单行纯文本，不得保留模板占位")
    if sections[0][1]:
        raise ValueError("报告标题下请直接保留总体总结标题")
    summary = content(sections[1][1], "总体总结")
    intro = sections[2][1]
    position, highlights = 3, []
    while position < len(sections) and re.match(
        r"^### 问题 \d+：", sections[position][0]
    ):
        header, body = sections[position]
        match = re.fullmatch(r"### 问题 (\d+)：(.+)", header)
        if match is None or not match[2].strip():
            raise ValueError("重点问题标题不能为空")
        if int(match[1]) != len(highlights) + 1:
            raise ValueError("重点问题须从1开始连续编号")
        values = fields(
            content(body, "重点问题"),
            ["关联规则", "问题概述"],
            ["关联规则", "问题概述"],
        )
        ids = values["关联规则"].split("、")
        known = {r.rule_id for r in rule_set.rules}
        if len(set(ids)) != len(ids) or any(key not in known for key in ids):
            raise ValueError("重点问题关联规则重复或越界")
        highlights.append(
            ReportHighlight(
                title=content(match[2].strip(), "重点标题"),
                rule_ids=ids,
                summary=values["问题概述"],
            )
        )
        position += 1
    if highlights:
        if intro:
            raise ValueError("重点问题块不能与占位或无重点问题混用")
    elif content(intro, "重点问题") not in {"", "无重点问题。"}:
        raise ValueError("无重点时仅写无重点问题。否则按问题块填写")
    categories, decisions = [], {}
    for category, ids in report_groups(rule_set).items():
        if sections[position][1]:
            raise ValueError("类别标题下不填写分类总结或其他内容")
        position += 1
        rules = []
        for key in ids:
            body = content(sections[position][1], key)
            position += 1
            if not body:
                continue
            values = fields(
                body,
                ["结论", "实际发现", "审核说明", "复核建议", *SOURCE_FIELDS],
                ["结论", "实际发现", "审核说明"],
            )
            if values["结论"] not in RESULTS:
                raise ValueError(f"{key}结论非法")
            data = {
                "reason": values["审核说明"],
                "observation": values["实际发现"],
                "recommendation": values.get("复核建议"),
            }
            for label, (field, role) in SOURCE_FIELDS.items():
                if label not in values:
                    continue
                keys = values[label].split("、")
                if len(set(keys)) != len(keys) or any(
                    not k or k.strip() != k or "\n" in k for k in keys
                ):
                    raise ValueError("来源须为非空单行，不能重复或带空白")
                for source in keys:
                    if (field == "image_paths") != ("/" in source):
                        raise ValueError("图片路径与片段 ID 类型不符")
                    ledger.validate(source, role)
                data[field] = keys
            rule = RuleAuditDecision(
                rule_id=key, result=RESULTS[values["结论"]], summary=data
            )
            rules.append(rule)
            decisions[key] = rule
        categories.append(CategoryAuditDecision(category=category, rules=rules))
    for h in highlights:
        if all(key in decisions for key in h.rule_ids) and all(
            decisions[key].result == "compliant" for key in h.rule_ids
        ):
            raise ValueError("重点问题须关联至少一项非符合结论")
    # 自由撰写文字不能裸写内部标识；来源原文不在此校验范围，不删除证据内容。
    prose = [title, summary, *[v for h in highlights for v in (h.title, h.summary)]]
    prose += [
        v
        for r in decisions.values()
        for v in (r.summary.reason, r.summary.observation, r.summary.recommendation)
        if v
    ]
    internal_ids = (
        {r.rule_id for r in rule_set.rules}
        | {rule_set.project_id}
        | set(ledger.documents)
        | set(ledger.sources)
    )
    for parsed in ledger.documents.values():
        internal_ids.update(
            v
            for v in (
                parsed.document.project_id,
                parsed.document.bidder_id,
                parsed.document.task_id,
            )
            if v
        )
    pattern = (
        r"(?<![A-Za-z0-9_-])(?:"
        + "|".join(re.escape(key) for key in internal_ids)
        + r")(?![A-Za-z0-9_-])"
    )
    # 名称恰好含技术编号时，仍允许按真实文件名引用；不修改输出文字。
    names = sorted(
        {p.document.file_name for p in ledger.documents.values()}, key=len, reverse=True
    )

    def has_internal_id(value):
        for name in names:
            value = value.replace(name, "")
        return re.search(pattern, value)

    if any(has_internal_id(value) for value in prose):
        raise ValueError(
            "正文请使用文件名称和可读表述，内部 ID 仅放关联规则或来源行；不要改写真实业务编号或引文"
        )
    if final and missing:
        raise ValueError("报告未完成：" + "、".join(missing))
    return (
        AgentAuditDecision(
            report_title=title,
            summary=summary,
            categories=categories,
            highlights=highlights,
        )
        if not missing
        else None
    ), missing
