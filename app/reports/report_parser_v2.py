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


def _prose_resolver(rule_set, ledger, context=None):
    """仅转换冻结快照内的已知正文引用，保留关联键、完整文件名与登记引文。"""
    names = {}

    def register(key, label):
        if key:
            names.setdefault(key, set()).add(label)

    for index, rule in enumerate(rule_set.rules, 1):
        register(rule.rule_id, f"第4.{index}节")
    for source in rule_set.source_files:
        register(source.file_id, source.file_name)
    rule_name = getattr(context, "rule_library_name", None) or "所选规则库"
    evidence_name = getattr(context, "evidence_library_name", None) or "所选资料库"
    task_name = getattr(context, "task_name", None) or "当前审核任务"
    register(rule_set.project_id, rule_name)
    protected = {source.file_name for source in rule_set.source_files}
    for parsed in ledger.documents.values():
        doc = parsed.document
        register(doc.file_id, doc.file_name)
        register(doc.project_id, rule_name)
        register(doc.bidder_id, evidence_name)
        register(doc.task_id, task_name)
        protected.add(doc.file_name)
    for key, source in ledger.sources.items():
        register(
            key,
            " · ".join(
                v
                for v in (
                    source.file_name,
                    source.section,
                    "页 " + "、".join(map(str, source.page_numbers))
                    if source.page_numbers
                    else None,
                )
                if v
            ),
        )
        if source.quote:
            protected.add(source.quote)
    # 优先保护完整文件名/实际登记的引文，避免同形业务编号被替换。
    protected.discard("")
    safe = "|".join(re.escape(v) for v in sorted(protected, key=len, reverse=True))
    ids = "|".join(re.escape(v) for v in sorted(names, key=len, reverse=True))
    pattern = re.compile(
        (f"(?P<original>{safe})|" if safe else "")
        + rf"(?<![A-Za-z0-9_-])(?P<key>{ids})(?![A-Za-z0-9_-])"
    )

    def replace(match):
        key = match.group("key")
        if key is None:
            return match[0]
        values = names[key]
        if len(values) != 1:
            raise ValueError(f"内部引用 {key} 在冻结快照中存在歧义，请改用具体名称")
        return next(iter(values))

    return lambda value: pattern.sub(replace, value)


def restore_category_headings(text, rule_set):
    """仅补可唯一定位的固定分类标题；缺规则、重复、乱序或未知标题不猜。"""
    headings = list(re.finditer(r"^#{1,6} .+$", text, re.MULTILINE))
    ordinary = [m[0] for m in headings if not re.match(r"^### 问题 \d+：", m[0])]
    expected = re.findall(r"^#{1,6} .+$", skeleton(rule_set, 2), re.MULTILINE)
    categories = {h for h in expected[3:] if h.startswith("## ")}
    absent = categories - set(ordinary)
    if (
        not absent
        or not ordinary
        or not ordinary[0].startswith("# ")
        or ordinary[1:] != [h for h in expected[1:] if h not in absent]
    ):
        return text, []
    before = {expected[i + 1]: h for i, h in enumerate(expected) if h in absent}
    restored = []
    for match in reversed(headings):
        if match[0] in before:
            header = before[match[0]]
            text = text[: match.start()] + header + "\n\n" + text[match.start() :]
            restored.append(header[3:])
    return text, list(reversed(restored))


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


def rule_fields(body, key, ledger, *, progress=False):
    """单块校验供草稿门禁复用；没有来源行合法，有值则严格校验。"""
    business = ["结论", "实际发现", "审核说明"]
    internal = ["核验状态", "进度说明"] if progress else []
    try:
        values = fields(body, [*internal, *business, "复核建议", *SOURCE_FIELDS], internal or business)
        if PENDING in body or "〈" in body or "```" in body:
            raise ValueError("已填写规则块仍有模板占位或代码围栏")
        if progress:
            if values["核验状态"] not in {"未完成", "已完成"}:
                raise ValueError("核验状态只能是未完成或已完成")
            complete = values["核验状态"] == "已完成"
            if complete and list(values)[2:5] != business:
                raise ValueError("已完成块须按顺序填写结论、实际发现、审核说明")
        else:
            complete = True
        if "结论" in values and values["结论"] not in RESULTS:
            raise ValueError("结论非法：当前仅支持符合、不符合、人工复核，请根据已有事实修正")
        data = {"reason": values.get("审核说明"), "observation": values.get("实际发现"), "recommendation": values.get("复核建议")}
        for label, (field, role) in SOURCE_FIELDS.items():
            if label not in values:
                continue
            keys = values[label].split("、")
            if len(set(keys)) != len(keys) or any(not k or k.strip() != k or "\n" in k for k in keys):
                raise ValueError("来源须为非空单行，不能重复或带空白")
            for source in keys:
                if (field == "image_paths") != ("/" in source):
                    raise ValueError("图片路径与片段 ID 类型不符")
                try:
                    ledger.validate(source, role)
                except ValueError as exc:
                    raise ValueError(f"{key}的{label}：{exc}") from exc
            data[field] = keys
        return values, data, complete
    except ValueError as exc:
        raise ValueError(f"{key}：{exc}") from exc


def draft_rule_states(text, rule_set, ledger):
    """只检查各关联块，不要求报告其它部分先完成，也不比较写前写后。"""
    try:
        _, sections = report_structure(text, rule_set)
    except ValueError as exc:
        return {rule.rule_id: (None, str(exc)) for rule in rule_set.rules}
    bodies = dict(sections)
    result = {}
    for rule in rule_set.rules:
        try:
            values, _, _ = rule_fields(bodies[f"### {rule.rule_id}"], rule.rule_id, ledger, progress=True)
            result[rule.rule_id] = (values["核验状态"], None)
        except ValueError as exc:
            result[rule.rule_id] = (None, str(exc))
    return result


def report_structure(text, rule_set):
    """只校验固定骨架及规则身份；其它块尚未填写不阻止局部进度检查。"""
    text, _ = restore_category_headings(text, rule_set)
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
        mismatch = next(
            (
                (want, got)
                for want, got in zip(expected[1:], ordinary[1:])
                if want != got
            ),
            None,
        )
        hint = (
            f"；首个差异：应为 {mismatch[0]}，实际为 {mismatch[1]}"
            if mismatch
            else "；检查缺失/多余标题及重点问题的位置"
        )
        raise ValueError(
            "报告须有一个一级标题，类别或规则集合/顺序须与当前骨架一致" + hint
        )
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
    return headers, sections


def parse_v2(text, rule_set, ledger, *, final=False, report_context=None, progress=False):
    headers, sections = report_structure(text, rule_set)
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
            values, data, complete = rule_fields(body, key, ledger, progress=progress)
            if not complete:
                missing.append(key)
                continue
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
    # 原稿和关联键保留内部标识；正式结果中的自由文字按冻结快照确定性转换。
    resolve = _prose_resolver(rule_set, ledger, report_context)
    title, summary = resolve(title), resolve(summary)
    for highlight in highlights:
        highlight.title = resolve(highlight.title)
        highlight.summary = resolve(highlight.summary)
    for rule in decisions.values():
        rule.summary.reason = resolve(rule.summary.reason)
        rule.summary.observation = resolve(rule.summary.observation)
        if rule.summary.recommendation:
            rule.summary.recommendation = resolve(rule.summary.recommendation)
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
