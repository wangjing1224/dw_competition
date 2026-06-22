"""ChatModel prompt_only 结构化输出和单次格式修复契约测试。"""

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from pydantic import BaseModel

from app.llm.chat_model import invoke_structured
from app.llm.errors import InvalidStructuredOutput


class Output(BaseModel):
    value: int


class SequenceModel:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.messages = []

    async def ainvoke(self, messages):
        self.messages.append(messages)
        return AIMessage(content=next(self.responses))


@pytest.mark.asyncio
async def test_prompt_only_repairs_invalid_json_once():
    model = SequenceModel(
        [
            "值是 2，不是 JSON。",
            '{"value": 2}',
        ]
    )

    result = await invoke_structured(
        model,
        [HumanMessage(content="返回结果")],
        Output,
        "prompt_only",
    )

    assert result.value == 2
    assert len(model.messages) == 2
    assert model.messages[0] == [HumanMessage(content="返回结果")]
    assert "只修复格式" in model.messages[1][0].content


@pytest.mark.asyncio
async def test_prompt_only_reports_failed_repair_diagnostics():
    model = SequenceModel(["不是 JSON", "仍然不是 JSON"])

    with pytest.raises(InvalidStructuredOutput) as error:
        await invoke_structured(
            model,
            [HumanMessage(content="返回结果")],
            Output,
            "prompt_only",
        )

    assert "格式修复后仍不符合 Schema" in str(error.value)
    assert "response_chars=" in str(error.value)
