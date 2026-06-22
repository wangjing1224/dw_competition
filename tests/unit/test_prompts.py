"""Prompt 配置路径、运行时注入和缺失变量测试。"""

import pytest

from app.prompts.loader import load_prompt, load_tool_description, render_prompt


def test_rule_prompt_runtime_values_are_injected():
    rendered = render_prompt(
        "rule_extraction",
        project_id="project-test",
        source_context="技术要求：像素不低于640x480",
        output_schema={"type": "object"},
    )

    assert "project-test" in rendered
    assert "640x480" in rendered
    assert '"type": "object"' in rendered


def test_missing_prompt_variable_fails_fast():
    with pytest.raises(KeyError):
        render_prompt("rule_extraction", project_id="project-test")


def test_all_tool_descriptions_are_available():
    for name in (
        "search_bid_response",
        "retrieve_rule_source",
        "request_evidence_review",
        "search_evidence",
        "retrieve_evidence_source",
    ):
        assert load_tool_description(name)
