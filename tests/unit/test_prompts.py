"""Prompt 配置路径、System/Human 模板和工具说明测试。"""

import pytest

from app.prompts.loader import load_tool_description, render_prompt


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
        source_context="技术要求：像素不低于640x480",
    )
    assert "当前暂无规则" in system
    assert "640x480" not in system
    assert "640x480" in human
    assert "file-1" in human


def test_missing_prompt_variable_fails_fast():
    with pytest.raises(KeyError):
        render_prompt("rule_extraction")


def test_all_tool_descriptions_are_available():
    for name in (
        "search_bid_response",
        "search_tender_source",
        "request_evidence_review",
        "search_evidence",
        "search_rule_source",
        "submit_rule_delta",
        "view_image",
    ):
        assert load_tool_description(name)


@pytest.mark.parametrize(
    ("name", "schema_variable", "model_name"),
    [
        ("bid_agent", "agent_audit_decision_schema", "AgentAuditDecision"),
        (
            "evidence_subagent",
            "evidence_review_schema",
            "EvidenceReviewDecision",
        ),
    ],
)
def test_agent_prompt_contains_runtime_output_schema(name, schema_variable, model_name):
    rendered = render_prompt(
        name,
        **{
            schema_variable: {
                "type": "object",
                "title": model_name,
            }
        },
    )
    assert model_name in rendered
    assert f"${{{schema_variable}}}" not in rendered
