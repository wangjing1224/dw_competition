"""基于 LangChain ChatModel 的结构化输出调用辅助。"""

import json
import logging
from typing import Any, Literal

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langchain_core.language_models.chat_models import BaseChatModel
from pydantic import TypeAdapter, ValidationError

from app.llm.errors import InvalidStructuredOutput, ModelCallError
from app.llm.structured_output import validate_structured_content


StructuredOutputMode = Literal["json_schema", "json_object", "prompt_only"]
logger = logging.getLogger(__name__)


def _response_diagnostics(response: AIMessage) -> dict[str, Any]:
    """提取不含 API Key 的模型终止原因和 Token 使用信息。"""

    metadata = response.response_metadata or {}
    usage = response.usage_metadata or {}
    return {
        "finish_reason": metadata.get("finish_reason"),
        "model_name": metadata.get("model_name"),
        "input_tokens": usage.get("input_tokens"),
        "output_tokens": usage.get("output_tokens"),
    }


async def _repair_structured_output(
    model: BaseChatModel,
    content: str,
    output_type: Any,
    validation_error: InvalidStructuredOutput,
) -> Any:
    """只修复 JSON 结构一次，不重新执行原业务判断。"""

    schema = TypeAdapter(output_type).json_schema()
    repair_message = HumanMessage(
        content=(
            "下面是上一次模型输出。它没有通过 JSON Schema 校验。"
            "请只修复格式、字段层级、枚举值和缺失字段，不添加新的业务事实。"
            "仅返回修复后的 JSON，不要输出 Markdown 或解释。\n\n"
            f"校验错误：\n{validation_error}\n\n"
            f"目标 JSON Schema：\n{json.dumps(schema, ensure_ascii=False)}\n\n"
            f"待修复输出：\n{content}"
        )
    )
    repaired = await model.ainvoke([repair_message])
    if not isinstance(repaired, AIMessage) or not isinstance(
        repaired.content, str
    ):
        raise InvalidStructuredOutput("格式修复调用没有返回文本 AIMessage")
    try:
        return validate_structured_content(repaired.content, output_type)
    except InvalidStructuredOutput as exc:
        diagnostics = _response_diagnostics(repaired)
        raise InvalidStructuredOutput(
            "格式修复后仍不符合 Schema；"
            f"finish_reason={diagnostics['finish_reason']}，"
            f"response_chars={len(repaired.content)}；{exc}"
        ) from exc


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

        # prompt_only 的业务 Prompt 已显式注入完整 Schema。这里直接调用普通
        # LangChain ChatModel，避免重复附加第二份 Schema 浪费上下文。
        response = await model.ainvoke(messages)
        if not isinstance(response, AIMessage):
            raise InvalidStructuredOutput("ChatModel 没有返回 AIMessage")
        content = response.content
        if not isinstance(content, str):
            raise InvalidStructuredOutput("模型响应不是文本 JSON")
        try:
            return validate_structured_content(content, output_type)
        except InvalidStructuredOutput as exc:
            diagnostics = _response_diagnostics(response)
            logger.warning(
                "structured output invalid, attempting one repair",
                extra={
                    **diagnostics,
                    "response_chars": len(content),
                    "output_type": getattr(output_type, "__name__", str(output_type)),
                },
            )
            return await _repair_structured_output(
                model,
                content,
                output_type,
                exc,
            )
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
