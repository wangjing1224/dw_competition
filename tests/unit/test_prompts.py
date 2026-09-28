"""Prompt 配置路径、System/Human 模板和工具说明测试。"""

import pytest

from app.prompts.loader import load_tool_description, render_prompt


def test_evidence_ingestion_prompt_separates_business_relations_from_similarity():
    system = render_prompt(
        "evidence_ingestion",
        current_material="尚无已保存结果",
        history_view="历史资料目录",
    )
    assert "具体业务事项" in system
    assert "仅主体/供方相同" in system
    assert "不是关系枚举" in system
    assert "简短短语" in system and "evidence_summary" in system
    assert "处理完成状态不写入摘要" in system
    assert "补充、证明、更正或有依据的矛盾" in system
    assert "不得丢掉当前材料自身的图片回查入口" in system


def test_evidence_save_schema_keeps_free_relations_and_matching_guidance():
    from app.models.evidence import EvidenceMaterialInput

    description = load_tool_description("save_evidence_material")
    assert "无明确业务联系填 []" in description
    assert "简短短语" in description
    assert "运行状态" in description
    assert "必须在 evidence_summary 中保留对应真实图片路径" in description
    schema = EvidenceMaterialInput.model_json_schema()
    relations = schema["properties"]["relations"]
    assert "仅同一主体不建立关系" in relations["description"]
    assert "enum" not in str(relations) and "maxLength" not in str(relations)
    assert set(schema["properties"]) == {"bid_summary", "evidence_summary", "relations"}


def test_rule_system_and_input_are_separate():
    system = render_prompt(
        "rule_extraction",
        rule_view="当前暂无规则",
    )
    human = render_prompt(
        "rule_extraction_input",
        file_id="file-1",
        file_name="技术规范.md",
        source_path="规则/技术规范.md",
        history_files="暂无已处理历史文件",
        reading_scope="第 1 批；还有后续内容：否",
        continuation="这是当前文件的首批内容。",
        source_context="技术要求：像素不低于640x480",
    )
    assert "当前暂无规则" in system
    assert "640x480" not in system
    assert "640x480" in human
    assert "file-1" in human


def test_missing_prompt_variable_fails_fast():
    with pytest.raises(KeyError):
        render_prompt("rule_extraction")


def test_audit_report_template_is_a_standalone_static_fragment():
    # 报告模板独立维护，由主 Agent System 引用。
    rendered = render_prompt("audit_report_template")
    assert "${" not in rendered
    assert "# 〈请填写报告标题〉" in rendered
    assert "一级标题由你" in rendered
    assert "## 总体总结" in rendered
    assert "### 分类总结" in rendered
    assert "### 〈rule_id〉" in rendered
    for label in (
        "结论：",
        "审核说明：",
        "招标片段：",
        "投标片段：",
        "资料片段：",
        "图片来源：",
    ):
        assert label in rendered
    assert "AgentAuditDecision" not in rendered


def test_all_tool_descriptions_are_available():
    for name in (
        "request_evidence_review",
        "report_document",
        "search_documents",
        "save_evidence_material",
        "submit_rule_delta",
        "view_image",
    ):
        assert load_tool_description(name)


def test_audit_system_uses_template_once_and_child_has_no_json_schema():
    template = render_prompt("audit_report_template")
    system = render_prompt(
        "bid_agent", rule_view="规则视图", report_markdown_contract=template
    )
    assert system.count(template) == 1
    assert "AgentAuditDecision" not in system
    assert "规则视图" in system
    child = render_prompt("evidence_subagent")
    assert "EvidenceReviewDecision" not in child
    assert "JSON" in child
