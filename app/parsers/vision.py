"""有界、串行、可缓存的解析视觉补充；不绑定业务工具，不生成规则。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from collections import Counter
from html import unescape
from html.parser import HTMLParser
from typing import Literal

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langsmith import tracing_context
from pydantic import model_validator

from app.core.config import get_settings
from app.llm.factory import build_parser_vision_model
from app.models.common import StrictModel
from app.models.documents import ParsedDocument
from app.parsers.assets import AssetStore, atomic_write, build_image_content
from app.parsers.common import refresh_status


class ImageReading(StrictModel):
    status: Literal["readable", "partial", "unreadable", "skipped"]
    content: str
    reason: str

    @model_validator(mode="after")
    def check_content(self):
        if self.status in {"readable", "partial"} and not self.content.strip():
            raise ValueError("可读/部分可读必须有转写内容")
        if self.status != "readable" and not self.reason.strip():
            raise ValueError("必须说明读取限制或跳过原因")
        if self.status == "readable" and "[无法辨认]" in self.content:
            raise ValueError("存在无法辨认内容时不能标记完全可读")
        if self.status in {"skipped", "unreadable"} and self.content.strip():
            raise ValueError("跳过/不可读不能同时输出转写事实")
        return self


class TableDecision(StrictModel):
    action: Literal["merge", "separate", "uncertain"]
    html: str
    reason: str

    @model_validator(mode="after")
    def validate_decision(self):
        if not self.reason.strip():
            raise ValueError("续表判断必须说明依据")
        if self.action == "merge" and not self.html.strip():
            raise ValueError("合并结果不能为空")
        if self.action != "merge" and self.html.strip():
            raise ValueError("未合并时不能提供替代表格")
        return self


class TableHTML(HTMLParser):
    """模型 HTML 白名单和基础网格检查；结构通过不代表事实已经验证。"""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack, self.cells, self.rows, self.text = [], [], [], []
        self.row = None

    def handle_starttag(self, tag, attrs):
        if tag not in {"table", "thead", "tbody", "tfoot", "tr", "td", "th", "br"}:
            raise ValueError("不允许的表格标签")
        for key, value in attrs:
            if (
                tag not in {"td", "th"}
                or key not in {"rowspan", "colspan"}
                or not value
                or not value.isdigit()
                or not 1 <= int(value) <= 2000
            ):
                raise ValueError("不允许的表格属性")
        parent = self.stack[-1] if self.stack else None
        if tag == "br":
            if parent not in {"td", "th"}:
                raise ValueError("换行必须在单元格内")
            self.text.append("\n")
            return
        allowed = {
            "table": {None},
            "thead": {"table"},
            "tbody": {"table"},
            "tfoot": {"table"},
            "tr": {"table", "thead", "tbody", "tfoot"},
            "td": {"tr"},
            "th": {"tr"},
        }
        if parent not in allowed[tag]:
            raise ValueError("表格层级错误")
        self.stack.append(tag)
        if tag == "tr":
            self.row = []
            self.rows.append(self.row)
        elif tag in {"td", "th"}:
            values = dict(attrs)
            self.row.append(
                (int(values.get("rowspan", 1)), int(values.get("colspan", 1)))
            )

    def handle_endtag(self, tag):
        if tag == "br":
            return
        if not self.stack or self.stack.pop() != tag:
            raise ValueError("表格标签不配对")
        self.text.append(" ")

    def handle_data(self, data):
        if data.strip() and (not self.stack or self.stack[-1] not in {"td", "th"}):
            raise ValueError("表格之外存在文字")
        self.text.append(data)

    def validate(self, html):
        if len(html) > 200_000:
            raise ValueError("表格输出过大")
        self.feed(html)
        self.close()
        if self.stack or not self.rows or len(self.rows) > 2000:
            raise ValueError("表格不完整")
        occupied = set()
        widths = []
        for r, row in enumerate(self.rows):
            col = 0
            for rs, cs in row:
                while (r, col) in occupied:
                    col += 1
                if col + cs > 200 or r + rs > len(self.rows):
                    raise ValueError("表格跨度越界")
                for y in range(r, r + rs):
                    for x in range(col, col + cs):
                        if (y, x) in occupied:
                            raise ValueError("表格单元格重叠")
                        occupied.add((y, x))
                col += cs
            widths.append(max((x + 1 for y, x in occupied if y == r), default=0))
        if (
            not widths[0]
            or len(set(widths)) != 1
            or len(occupied) != widths[0] * len(widths)
        ):
            raise ValueError("表格行列结构不一致")
        return "".join(self.text)


def validate_transcription(content: str) -> None:
    # 表格 HTML 以外保留 Markdown 文本；拒绝可执行标签/地址，不把输出渲染成脚本。
    if re.search(r"<(script|iframe|img|a|object|style)\b", content, re.I):
        raise ValueError("视觉输出含不允许的 HTML")
    tables = re.findall(r"<table\b[^>]*>.*?</table>", content, flags=re.I | re.S)
    if len(re.findall(r"<table\b", content, flags=re.I)) != len(tables):
        raise ValueError("表格被截断")
    for table in tables:
        TableHTML().validate(table)
    remaining = re.sub(r"<table\b[^>]*>.*?</table>", "", content, flags=re.I | re.S)
    if re.search(r"</?[A-Za-z][^>]*>", remaining):
        raise ValueError("转写正文不允许其他 HTML 标签")
    if re.search(r"\]\(\s*(?:javascript|data|file):", remaining, re.I):
        raise ValueError("转写包含不安全链接")


def validate_merged_cells(originals: list[str], merged: str) -> None:
    """保守核对行内值与重复次数；不能证明无损时保持原页，不静默丢重复数据行。"""

    def rows(html):
        result = []
        for row in re.findall(r"<tr\b[^>]*>(.*?)</tr>", html, re.S):
            cells = re.findall(r"<(?:td|th)\b[^>]*>(.*?)</(?:td|th)>", row, re.S)
            result.append(
                [re.sub(r"\s+", "", unescape(re.sub(r"<[^>]+>", "", c))) for c in cells]
            )
        return result

    left, right = [rows(html) for html in originals]
    actual = rows(merged)
    expected = Counter(c for row in [*left, *right] for c in row)
    observed = Counter(c for row in actual for c in row)
    if expected == observed:
        return
    # 只允许模型确认的单行重复表头；不沿相同前缀继续删除实际数据行。
    # 多行表头或跨页断行无法保守核对时保留原表，不能为了接受结果放松校验。
    for a, b in zip(left[:1], right[:1]):
        if a != b:
            break
        expected.subtract(a)
        if +expected == +observed:
            return
    raise ValueError("合并改变了单元格内容或数量，保留原页等待回查")


async def _request(model, messages, output_type):
    # 图片/敏感原文不默认上传到第三方 Trace；不打印完整请求和响应。
    with tracing_context(enabled=False):
        response = await model.ainvoke(messages)
    if not isinstance(response, AIMessage) or not isinstance(response.content, str):
        raise ValueError("视觉模型未返回文本 JSON")
    if response.response_metadata.get("finish_reason") not in {None, "stop"}:
        raise ValueError("视觉输出未正常结束，不接受截断结果")
    content = response.content.strip()
    if content.startswith("```json") and content.endswith("```"):
        content = content[7:-3].strip()
    # 不从残缺输出中搜第一个合法对象，也不脱离图片另调用模型补写事实。
    return output_type.model_validate(json.loads(content))


def _cache_key(prompt, settings, image_bytes, context=""):
    config = settings.parser_vision_model
    identity = {
        "parser": "task19-v1",
        "prompt": prompt,
        "context": context,
        "model": config.model_dump(mode="json", exclude={"api_key"}) if config else {},
        "max_side": settings.parsing.vision_image_max_side,
    }
    digest = hashlib.sha256(
        json.dumps(identity, ensure_ascii=False, sort_keys=True).encode()
    )
    for data in image_bytes:
        digest.update(hashlib.sha256(data).digest())
    return digest.hexdigest()


async def enrich_document(parsed: ParsedDocument, assets: AssetStore, *, model=None):
    settings = get_settings()
    work = parsed.model_copy(deep=True)
    images = [
        i
        for s in work.sections
        for b in s.blocks
        for i in b.images
        if i.purpose == "content"
    ]
    candidates = table_candidates(work)
    if not images and not candidates:
        return work
    if model is None:
        try:
            model = build_parser_vision_model(settings)
        except ValueError:
            work.warnings.append(
                "需要视觉辅助但 parser_vision_model 配置缺失，相关图片/表格未完成读取"
            )
            return refresh_status(work)
    prompt_dir = settings.project_root / "configs/prompts/templates"
    prompt = (prompt_dir / "parser_image_transcription.md").read_text("utf-8")
    for image in images:
        if image.read_status in {"readable", "skipped"}:
            continue
        try:
            raw = assets.read_image(image)
            key = _cache_key(prompt, settings, [raw])
            path = assets.root / "vision-cache" / f"{key}.json"
            reading = None
            if path.exists():
                try:
                    reading = ImageReading.model_validate_json(path.read_text("utf-8"))
                    validate_transcription(reading.content)
                except ValueError:
                    reading = None
            if reading is None:
                reading = await _request(
                    model,
                    [
                        SystemMessage(content=prompt),
                        HumanMessage(
                            content=[
                                {"type": "text", "text": "请忠实读取这张材料图片。"},
                                build_image_content(
                                    work,
                                    image.path,
                                    assets,
                                    max_side=settings.parsing.vision_image_max_side,
                                ),
                            ]
                        ),
                    ],
                    ImageReading,
                )
                validate_transcription(reading.content)
                if reading.status in {"readable", "skipped"}:
                    await asyncio.to_thread(
                        atomic_write, path, reading.model_dump_json().encode()
                    )
            image.transcription = reading.content
            image.read_status = reading.status
            image.warning = reading.reason or None
        except Exception as exc:
            image.read_status = "unreadable"
            image.warning = f"视觉读取未完成：{type(exc).__name__}，保留原图待重试"
        await _checkpoint(work, assets)
    await merge_tables(work, assets, model, settings, prompt_dir)
    return refresh_status(work)


async def _checkpoint(parsed, assets):
    # 每项进展可见，但不切换对外已发布版本；重试通过成功缓存复用。
    digest = hashlib.sha256(parsed.model_dump_json().encode()).hexdigest()
    await asyncio.to_thread(
        atomic_write,
        assets.root / "checkpoints" / f"{digest}.json",
        parsed.model_dump_json(indent=2).encode(),
    )


def table_candidates(parsed):
    """只找相邻页边界的候选，不以候选筛选结果代替模型判断。"""
    result = []
    sections = parsed.sections
    for left, right in zip(sections, sections[1:]):
        a = next((b for b in reversed(left.blocks) if b.kind == "table"), None)
        b = next((b for b in right.blocks if b.kind == "table"), None)
        if not a or not b or not a.page_sources or not b.page_sources:
            continue
        x, y = a.page_sources[-1], b.page_sources[0]
        if y.page_number != x.page_number + 1 or not x.bbox or not y.bbox:
            continue
        # 保守宽度筛选；表前/表后上下文会随请求提供供模型判定。
        wa, wb = x.bbox[2] - x.bbox[0], y.bbox[2] - y.bbox[0]
        if min(wa, wb) / max(wa, wb) >= 0.85 and a.images and b.images:
            result.append((left, right, a, b))
    return result


async def merge_tables(parsed, assets, model, settings, prompt_dir):
    prompt = (prompt_dir / "parser_table_merge.md").read_text("utf-8")
    # 按页重新寻找左侧合并表，可连续处理三页及以上；不重写整个 PDF。
    merged_into = {}
    for left, right, original_a, b in table_candidates(parsed):
        a = merged_into.get(id(original_a), original_a)
        if len(a.page_sources) > 2:
            # 当前接口最多给三张回查图，不隐式让模型仅看末页却重写全部已合并内容。
            a.warnings.append("长续表超过当前局部视觉合并范围，保留剩余页待人工回查")
            b.warnings.append("可能为长续表后续页，当前尚未确认完整表格范围")
            continue
        context = json.dumps(
            {
                "previous_section": left.title,
                "next_section": right.title,
                "previous_text": [v.content for v in left.blocks if v.kind == "text"],
                "next_text": [v.content for v in right.blocks if v.kind == "text"],
                "tables": [a.content, b.content],
            },
            ensure_ascii=False,
        )
        refs = list(
            dict.fromkeys(
                s.image_path
                for block in [a, b]
                for s in block.page_sources
                if s.image_path
            )
        )
        try:
            if not refs:
                raise ValueError("缺失原图")
            images_by_path = {i.path: i for block in [a, b] for i in block.images}
            key = _cache_key(
                prompt,
                settings,
                [assets.read_image(images_by_path[p]) for p in refs],
                context,
            )
            path = assets.root / "vision-cache" / f"table-{key}.json"
            decision = None
            if path.exists():
                try:
                    decision = TableDecision.model_validate_json(
                        path.read_text("utf-8")
                    )
                except ValueError:
                    pass
            if decision is None:
                decision = await _request(
                    model,
                    [
                        SystemMessage(content=prompt),
                        HumanMessage(
                            content=[
                                {"type": "text", "text": context},
                                *[
                                    build_image_content(
                                        parsed,
                                        p,
                                        assets,
                                        max_side=settings.parsing.vision_image_max_side,
                                    )
                                    for p in refs
                                ],
                            ]
                        ),
                    ],
                    TableDecision,
                )
            if decision.action == "merge":
                merged_text = TableHTML().validate(decision.html)
                originals = [TableHTML().validate(block.content) for block in [a, b]]
                validate_merged_cells([a.content, b.content], decision.html)
                # 不允许新增数字或删除已有数字种类；额外做原单元格文字保留检查。
                tokens = lambda text: Counter(re.findall(r"[+-]?\d+(?:\.\d+)?", text))
                if set(tokens(merged_text)) != set(tokens(" ".join(originals))):
                    raise ValueError("合并表数字发生变化")
                for block in [a, b]:
                    cells = re.findall(
                        r"<(?:td|th)\b[^>]*>(.*?)</(?:td|th)>", block.content, re.S
                    )
                    for cell in cells:
                        text = re.sub(
                            r"\s+", "", unescape(re.sub(r"<[^>]+>", "", cell))
                        )
                        if text and text not in re.sub(r"\s+", "", merged_text):
                            raise ValueError("合并表遗漏原单元格内容")
                # 先落成功缓存再更新工作副本；缓存写失败时原页仍在。
                await asyncio.to_thread(
                    atomic_write, path, decision.model_dump_json().encode()
                )
                a.content = decision.html
                a.content_origin = "vision_assisted"
                a.images = list({i.path: i for i in [*a.images, *b.images]}.values())
                a.page_sources = [*a.page_sources, *b.page_sources]
                a.warnings.extend(b.warnings)
                right.blocks.remove(b)
                merged_into[id(b)] = a
            elif decision.action == "uncertain":
                a.warnings.append("跨页关系未确认：" + decision.reason)
                b.warnings.append("跨页关系未确认，保留本页原始表格")
            if decision.action == "separate":
                await asyncio.to_thread(
                    atomic_write, path, decision.model_dump_json().encode()
                )
        except Exception as exc:
            a.warnings.append(f"续表合并未通过校验：{type(exc).__name__}，保留各页原表")
            b.warnings.append("与上一页的续表关系未解决，不能视为独立完整表")
        await _checkpoint(parsed, assets)
