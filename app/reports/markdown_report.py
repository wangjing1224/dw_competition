"""报告层负责骨架和业务校验；文本工具只负责读写当前稿。"""

import asyncio
import re
from pathlib import Path

from app.models.tasks import (
    AgentAuditDecision,
    CategoryAuditDecision,
    RuleAuditDecision,
)
from app.parsers.assets import atomic_write
from app.reports.styles import category_label

RESULTS = {
    "符合": "compliant",
    "不符合": "non_compliant",
    "风险": "risk",
    "人工复核": "manual_review",
}
SOURCE_FIELDS = {
    "招标片段": ("tender_chunk_ids", "rule_source"),
    "投标片段": ("bid_chunk_ids", "bid_document"),
    "资料片段": ("evidence_chunk_ids", "evidence_material"),
    "图片来源": ("image_paths", None),
}
PENDING = "待填写。"


def report_groups(rule_set):
    groups = {}
    for rule in rule_set.rules:
        groups.setdefault(str(rule.category), []).append(rule.rule_id)
    return groups


def skeleton(rule_set):
    parts = ["# 当前投标文件审核报告", "## 总体总结", PENDING]
    for category, ids in report_groups(rule_set).items():
        parts.extend(
            [f"## {category_label(category)}（{category}）", "### 分类总结", PENDING]
        )
        for rule_id in ids:
            parts.extend([f"### {rule_id}", PENDING])
    return "\n\n".join(parts) + "\n"


def parse_report(text, rule_set, ledger, *, final=False):
    """只解析本模板，不猜测自由文章；草稿保留未填块，已填块完整校验。"""
    expected = skeleton(rule_set)
    heading = re.compile(r"^#{1,6} .+$", re.MULTILINE)
    actual_headers = heading.findall(text)
    if actual_headers != heading.findall(expected):
        raise ValueError("报告标题、类别或规则集合/顺序与当前骨架不一致，请读取当前稿")
    if text[: heading.search(text).start()].strip():
        raise ValueError("报告标题前不能有其他内容或代码围栏")
    matches = list(heading.finditer(text))
    bodies = [
        text[
            m.end() : matches[i + 1].start() if i + 1 < len(matches) else len(text)
        ].strip()
        for i, m in enumerate(matches)
    ]
    if bodies[0]:
        raise ValueError("报告标题下请直接保留总体总结标题")
    missing = []

    def content(body, label):
        if not body or body == PENDING:
            missing.append(label)
            return ""
        if PENDING in body or "〈" in body or "```" in body:
            raise ValueError(f"{label}仍包含模板占位或代码围栏")
        return body

    summary = content(bodies[1], "总体总结")
    categories = []
    position = 2
    for category, ids in report_groups(rule_set).items():
        if bodies[position]:
            raise ValueError("类别标题下请使用分类总结，不放未约定字段")
        category_summary = content(
            bodies[position + 1], f"{category_label(category)}分类总结"
        )
        position += 2
        decisions = []
        for rule_id in ids:
            body = content(bodies[position], rule_id)
            position += 1
            if not body:
                continue
            fields = list(
                re.finditer(
                    r"^(结论|审核说明|招标片段|投标片段|资料片段|图片来源)：",
                    body,
                    re.MULTILINE,
                )
            )
            labels = [m[1] for m in fields]
            if (
                labels[:2] != ["结论", "审核说明"]
                or len(labels) != len(set(labels))
                or fields[0].start()
            ):
                raise ValueError(f"{rule_id}须有唯一结论、审核说明及合法来源行")
            values = {
                m[1]: body[
                    m.end() : fields[i + 1].start()
                    if i + 1 < len(fields)
                    else len(body)
                ].strip()
                for i, m in enumerate(fields)
            }
            if values["结论"] not in RESULTS or not values["审核说明"]:
                raise ValueError(f"{rule_id}结论非法或说明为空")
            data = {"reason": values["审核说明"]}
            for label, (field, role) in SOURCE_FIELDS.items():
                if label not in values:
                    continue
                value = values[label]
                if not value or "\n" in value:
                    raise ValueError(f"{rule_id}的{label}须为非空单行 ID/路径")
                ids_used = value.split("、")
                if any(not x or x.strip() != x for x in ids_used) or len(
                    set(ids_used)
                ) != len(ids_used):
                    raise ValueError("来源重复或空白")
                for key in ids_used:
                    if (field == "image_paths") != ("/" in key):
                        raise ValueError("图片路径与片段 ID 类型不符")
                    ledger.validate(key, role)
                data[field] = ids_used
            decisions.append(
                RuleAuditDecision(
                    rule_id=rule_id, result=RESULTS[values["结论"]], summary=data
                )
            )
        if category_summary:
            categories.append(
                CategoryAuditDecision(
                    category=category, summary=category_summary, rules=decisions
                )
            )
    if final and missing:
        raise ValueError("报告未完成：" + "、".join(missing))
    result = (
        AgentAuditDecision(summary=summary, categories=categories)
        if not missing
        else None
    )
    return result, missing


class ReportDocument:
    """一个文件一份稿。保存先验证候选，成功原子发布；错误不污染旧稿。"""

    def __init__(self, path: Path, rule_set, ledger):
        self.path, self.rule_set, self.ledger = path, rule_set, ledger
        self.lock = asyncio.Lock()
        self.receipts = {}
        self.batch_key = 0
        self.write_error = None
        # 新审核不偷偷复用另一轮的草稿；恢复只由显式文件会话负责。
        if path.exists():
            raise ValueError(f"审核草稿已存在，不覆盖：{path}")
        atomic_write(path, skeleton(rule_set).encode("utf-8"))

    def read(self):
        return self.path.read_text("utf-8")

    def start_batch(self, number):
        # 新 graph 会话的模型可能重新从 call_0 编号；同批压缩续跑则不能清零。
        self.batch_key = number

    def missing(self):
        return parse_report(self.read(), self.rule_set, self.ledger)[1]

    async def apply(self, action, content, edits, call_id):
        async with self.lock:
            key = (self.batch_key, call_id)
            payload = (action, content, [edit.model_dump() for edit in edits or []])
            if key in self.receipts:
                previous, receipt = self.receipts[key]
                if previous != payload:
                    raise ValueError(
                        "同一工具调用 ID 不能用于不同的写入，请使用新的调用"
                    )
                return receipt
            if action == "read":
                return (
                    self.read() + "\n\n报告缺项：" + ("、".join(self.missing()) or "无")
                )
            try:
                candidate = content if action == "write" else self.read()
                for edit in edits or []:
                    if candidate.count(edit.old_text) != 1:
                        raise ValueError(
                            "old_text 未唯一命中，整批未保存；请先 read 再精确修改"
                        )
                    candidate = candidate.replace(edit.old_text, edit.new_text, 1)
                _, missing = parse_report(candidate, self.rule_set, self.ledger)
                atomic_write(self.path, candidate.encode("utf-8"))
                result = "已保存。" + (f"完成{len(edits)}处替换。" if edits else "")
                result += "\n报告缺项：" + ("、".join(missing) or "无")
                self.receipts[key] = (payload, result)
                self.write_error = None
                return result
            except Exception as exc:
                self.write_error = str(exc)
                raise

    def finish(self):
        if self.write_error:
            raise ValueError("报告存在未解决的保存错误：" + self.write_error)
        return parse_report(self.read(), self.rule_set, self.ledger, final=True)[0]
