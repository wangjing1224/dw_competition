"""基于 LangChain ChatModel 的结构化输出调用辅助。"""

from typing import Any, Literal

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langchain_core.language_models.chat_models import BaseChatModel
from pydantic import TypeAdapter, ValidationError

from app.llm.errors import InvalidStructuredOutput, ModelCallError
from app.llm.structured_output import validate_structured_content


StructuredOutputMode = Literal["json_schema", "json_object", "prompt_only"]


def append_schema_instruction(
    messages: list[BaseMessage], output_type: Any
) -> list[BaseMessage]:
    schema = TypeAdapter(output_type).json_schema()
    instruction = (
        "请仅返回符合以下 JSON Schema 的 JSON，不要包含 Markdown：\n"
        f"{schema}"
    )
    return [*messages, HumanMessage(content=instruction)]


async def invoke_structured(
    model: BaseChatModel,
    messages: list[BaseMessage],
    output_type: Any,
    mode: StructuredOutputMode,
) -> Any:
    try:
        if mode in {"json_schema", "json_object"}:
            method = "json_schema" if mode == "json_schema" else "json_mode"
            structured = model.with_structured_output(output_type, method=method)
            return await structured.ainvoke(messages)

        response = await model.ainvoke(
            append_schema_instruction(messages, output_type)
        )
        if not isinstance(response, AIMessage):
            raise InvalidStructuredOutput("ChatModel 没有返回 AIMessage")
        content = response.content
        if not isinstance(content, str):
            raise InvalidStructuredOutput("模型响应不是文本 JSON")
        return validate_structured_content(content, output_type)
    except InvalidStructuredOutput:
        raise
    except ValidationError as exc:
        raise InvalidStructuredOutput(str(exc)) from exc
    except TimeoutError as exc:
        raise ModelCallError("model_timeout", str(exc), retryable=True) from exc
    except Exception as exc:
        raise ModelCallError(
            "model_call_failed",
            str(exc),
            retryable=False,
        ) from exc
