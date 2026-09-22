"""图片转写的展示与存储分离；不调用模型。"""

import pytest

from app.models.documents import (
    DocumentRef,
    ParsedBlock,
    ParsedDocument,
    ParsedImage,
    ParsedSection,
)
from app.parsers import render, service


def image_block(text, *, status="readable", warning=None):
    return ParsedBlock(
        kind="image",
        source_block_index=0,
        images=[
            ParsedImage(
                path="images/1.png",
                mime_type="image/png",
                width=1,
                height=1,
                transcription=text,
                read_status=status,
                warning=warning,
            )
        ],
    )


@pytest.mark.parametrize(
    "before,after",
    [
        ("# 图题\n## 子标题\n比例8%", "### 图题\n#### 子标题\n比例8%"),
        ("## 图题\n### 子标题", "### 图题\n#### 子标题"),
        ("### 图题\n#### 子标题", "### 图题\n#### 子标题"),
        ("# 图题\n###### 小标题", "### 图题\n###### 小标题"),
        ("   # 图题 ###\r\n正文", "   ### 图题 ###\r\n正文"),
        ("#123不是标题\n编号 #8", "#123不是标题\n编号 #8"),
        (
            "| 类别 | 占比 |\n|---|---|\n| #标记 | 8% |",
            "| 类别 | 占比 |\n|---|---|\n| #标记 | 8% |",
        ),
    ],
)
def test_transcription_heading_levels_without_changing_original(before, after):
    block = image_block(before)
    original = block.model_dump_json()
    assert f"[图片解读，非原生文本]\n\n{after}" in render.render_block(block)
    assert block.model_dump_json() == original
    assert render._render_transcription(after) == after


def test_code_and_html_cells_are_not_treated_as_headings():
    literal = "```text\n# 代码原文\n```\n~~~\n## 另一段原文\n~~~\n<table>\n<tr><td>\n# 表格内文字\n</td></tr>\n</table>"
    block = image_block("# 图题\n" + literal)
    text = render.render_block(block)
    assert "### 图题\n" + literal in text


@pytest.mark.parametrize(
    "status,label",
    [
        ("readable", "读取说明"),
        ("partial", "读取限制"),
        ("unreadable", "读取限制"),
        ("skipped", "跳过原因"),
        ("failed", "处理失败原因"),
        ("pending", "读取限制"),
    ],
)
def test_reading_reason_label_follows_state_and_keeps_text(status, label):
    block = image_block("原始信息", status=status, warning="模型原始说明")
    result = render.render_block(block)
    assert f"{label}：模型原始说明" in result
    assert block.images[0].warning == "模型原始说明"
    if status == "readable":
        assert "读取限制：" not in result


def test_render_change_creates_new_snapshot_without_overwriting_old(
    temp_settings, monkeypatch
):
    monkeypatch.setattr(service, "get_settings", lambda: temp_settings)
    parsed = ParsedDocument(
        schema_version=2,
        parse_status="parsed",
        document=DocumentRef(
            file_id="file-1",
            file_name="source.txt",
            source_path="source.txt",
            project_id="project-1",
            data_role="rule_source",
        ),
        sections=[
            ParsedSection(
                section_id="s1",
                title="第一页",
                blocks=[ParsedBlock(kind="text", content="原文", source_block_index=0)],
            )
        ],
    )

    class Repository:
        def update_parse_status(self, file_id, status):
            pass

    first = service.persist_parse_result(parsed, Repository())
    old_path = temp_settings.storage.data_root / "parsed" / first.markdown_path
    old_bytes = old_path.read_bytes()
    monkeypatch.setattr(service, "render_document", lambda result: "新版显示\n")
    second = service.persist_parse_result(parsed, Repository())
    assert second.markdown_path != first.markdown_path
    assert old_path.read_bytes() == old_bytes
    assert (
        temp_settings.storage.data_root / "parsed" / second.markdown_path
    ).read_text() == "新版显示\n"
    assert (
        service.persist_parse_result(parsed, Repository()).markdown_path
        == second.markdown_path
    )
