"""Markdown JSON 清理和本地 Pydantic 校验测试。"""

import pytest
from pydantic import BaseModel

from app.llm.errors import InvalidStructuredOutput
from app.llm.structured_output import validate_structured_content


class Output(BaseModel):
    value: int


def test_markdown_json_is_validated():
    result = validate_structured_content("```json\n{\"value\": 2}\n```", Output)
    assert result.value == 2


def test_invalid_schema_is_rejected():
    with pytest.raises(InvalidStructuredOutput):
        validate_structured_content('{"value": "x"}', Output)
