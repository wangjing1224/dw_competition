"""Prompt 配置路径、System/Human 模板和工具说明测试。"""

import pytest

from app.prompts.loader import load_tool_description, render_prompt


def test_rule_system_and_input_are_separate():
    system = render_prompt(
        "rule_extraction",
        rule_delta_schema={"type": "object"},
    )
    human = render_prompt(
        "rule_extraction_input",
        current_rules=[],
        source_file="规则/技术规范.md",
        batch_index=1,
        batch_count=2,
        source_context="技术要求：像素不低于640x480",
    )
    assert '"type": "object"' in system
    assert "640x480" not in system
    assert "640x480" in human
    assert "1/2" in human


def test_missing_prompt_variable_fails_fast():
    with pytest.raises(KeyError):
        render_prompt("rule_extraction")


def test_all_tool_descriptions_are_available():
    for name in (
        "search_bid_response",
        "search_tender_source",
        "request_evidence_review",
        "search_evidence",
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
def test_agent_prompt_contains_runtime_output_schema(
    name, schema_variable, model_name
):
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
