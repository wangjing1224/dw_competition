"""从模型文本中提取首个 JSON 值并执行 Pydantic 校验。"""

import json
import re
from typing import Any

from pydantic import TypeAdapter, ValidationError

from app.llm.errors import InvalidStructuredOutput


FENCE_PATTERN = re.compile(
    r"^\s*```(?:json)?\s*(.*?)\s*```\s*$",
    re.IGNORECASE | re.DOTALL,
)


def strip_markdown_fence(content: str) -> str:
    matched = FENCE_PATTERN.match(content)
    return matched.group(1) if matched else content.strip()


def extract_json_value(content: str) -> Any:
    text = strip_markdown_fence(content)
    decoder = json.JSONDecoder()
    for index, character in enumerate(text):
        if character not in "[{":
            continue
        try:
            value, _ = decoder.raw_decode(text[index:])
            return value
        except json.JSONDecodeError:
            continue
    raise InvalidStructuredOutput("模型响应中没有可解析的 JSON")


def validate_structured_content(content: str, output_type: Any) -> Any:
    try:
        return TypeAdapter(output_type).validate_python(extract_json_value(content))
    except ValidationError as exc:
        raise InvalidStructuredOutput(str(exc)) from exc
