"""Markdown JSON 清理和本地 Pydantic 校验测试。"""

import pytest
from pydantic import BaseModel

from app.llm.errors import InvalidStructuredOutput
from app.llm.structured_output import extract_json_value, validate_structured_content


class Output(BaseModel):
    value: int


def test_markdown_json_is_validated():
    result = validate_structured_content("```json\n{\"value\": 2}\n```", Output)
    assert result.value == 2


def test_invalid_schema_is_rejected():
    with pytest.raises(InvalidStructuredOutput):
        validate_structured_content('{"value": "x"}', Output)


def test_embedded_json_fence_is_preferred_over_explanatory_text():
    content = '先说明结论。\n```json\n{"value": 2}\n```'
    assert extract_json_value(content) == {"value": 2}


def test_broken_fenced_json_does_not_fall_back_to_inner_object():
    content = (
        '说明\n```json\n'
        '{"value": {"nested": 2}, "text": "投标行为"十不准""}'
        '\n```'
    )
    with pytest.raises(InvalidStructuredOutput, match="JSON 代码块语法错误"):
        extract_json_value(content)
