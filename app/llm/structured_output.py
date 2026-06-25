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
EMBEDDED_FENCE_PATTERN = re.compile(
    r"```(?:json)?\s*(.*?)\s*```",
    re.IGNORECASE | re.DOTALL,
)


def strip_markdown_fence(content: str) -> str:
    matched = FENCE_PATTERN.match(content)
    return matched.group(1) if matched else content.strip()


def extract_json_value(content: str) -> Any:
    fenced_blocks = EMBEDDED_FENCE_PATTERN.findall(content)
    if fenced_blocks:
        errors: list[str] = []
        for block in fenced_blocks:
            try:
                return json.loads(block)
            except json.JSONDecodeError as exc:
                errors.append(
                    f"第{exc.lineno}行第{exc.colno}列：{exc.msg}"
                )
        # 存在明确 JSON 代码块时，不再从损坏 JSON 的内部对象中误提取局部结果。
        raise InvalidStructuredOutput(
            "模型返回的 JSON 代码块语法错误：" + "；".join(errors)
        )

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
